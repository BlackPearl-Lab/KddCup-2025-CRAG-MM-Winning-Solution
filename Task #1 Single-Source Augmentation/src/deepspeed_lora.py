#!/usr/bin/env python
# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: Apache-2.0

# DeepSpeed Team
import argparse
import json
import pickle
import time
import os
import math
import sys
import pandas as pd
import pickle
from PIL import Image
from tqdm.auto import tqdm
import torch
from torch.utils.data import DataLoader, RandomSampler, SequentialSampler
from torch.utils.data.distributed import DistributedSampler
from transformers import set_seed, AutoConfig, AutoProcessor, MllamaForConditionalGeneration
import random
import numpy as np
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    SchedulerType,
    default_data_collator,
    get_scheduler,
    BitsAndBytesConfig,
    AutoModelForSequenceClassification
)
from transformers import optimization

import deepspeed
from deepspeed.ops.adam import DeepSpeedCPUAdam, FusedAdam

# from utils.sft_dataset import is_rank_0
import torch.distributed as dist
from peft import (
    LoraConfig,
    get_peft_model,
    get_peft_model_state_dict,
    set_peft_model_state_dict,
)
from peft import prepare_model_for_kbit_training
import torch.nn as nn
import torch.nn.functional as F
import string

sys.path.append(
    os.path.abspath(os.path.join(os.path.dirname(__file__), os.path.pardir)))
from torch.utils.data import Dataset

IGNORE_INDEX = -100

import torch
from torch import nn


def is_rank_0() -> bool:
    return not dist.is_initialized() or dist.get_rank() == 0


GLOBAL_BATCH_SIZE = 32
MICRO_BATCH_SIZE = 4


def get_train_ds_config(offload,
                        stage=2,
                        enable_hybrid_engine=False,
                        inference_tp_size=1,
                        release_inference_cache=False,
                        pin_parameters=True,
                        tp_gather_partition_size=8,
                        max_out_tokens=512,
                        enable_tensorboard=False,
                        enable_mixed_precision_lora=False,
                        tb_path="",
                        tb_name=""):
    device = "cpu" if offload else "none"
    zero_opt_dict = {
        "stage": stage,
        "offload_param": {
            "device": device
        },
        "offload_optimizer": {
            "device": device
        },
        "stage3_param_persistence_threshold": 1e4,
        "stage3_max_live_parameters": 3e7,
        "stage3_prefetch_bucket_size": 3e7,
        "memory_efficient_linear": False
    }
    if enable_mixed_precision_lora:
        zero_opt_dict["zero_quantized_nontrainable_weights"] = True
        zero_opt_dict["zero_hpz_partition_size"] = torch.cuda.device_count()
    return {
        "train_batch_size": GLOBAL_BATCH_SIZE,
        "train_micro_batch_size_per_gpu": MICRO_BATCH_SIZE,
        "steps_per_print": 10,
        "zero_optimization": zero_opt_dict,
        "fp16": {
            "enabled": False,
            "loss_scale_window": 100
        },
        "bfloat16": {
            "enabled": False,
            "loss_scale_window": 100
        },
        "gradient_clipping": 1.0,
        "prescale_gradients": False,
        "wall_clock_breakdown": False,
        "hybrid_engine": {
            "enabled": enable_hybrid_engine,
            "max_out_tokens": max_out_tokens,
            "inference_tp_size": inference_tp_size,
            "release_inference_cache": release_inference_cache,
            "pin_parameters": pin_parameters,
            "tp_gather_partition_size": tp_gather_partition_size,
        },
        "tensorboard": {
            "enabled": enable_tensorboard,
            "output_path": f"{tb_path}/ds_tensorboard_logs/",
            "job_name": f"{tb_name}_tensorboard"
        }
    }


def get_optimizer_grouped_parameters(
        model,
        weight_decay,
        lora_lr=5e-4,
        no_decay_name_list=["bias", "LayerNorm.weight"],
        lora_name_list=["lora_right_weight", "lora_left_weight"],
):
    optimizer_grouped_parameters = [
        {
            "params": [
                p for n, p in model.named_parameters()
                if (not any(nd in n for nd in no_decay_name_list)
                    and p.requires_grad and not any(nd in n
                                                    for nd in lora_name_list))
            ],
            "weight_decay":
                weight_decay,
        },
        {
            "params": [
                p for n, p in model.named_parameters()
                if (not any(nd in n for nd in no_decay_name_list)
                    and p.requires_grad and any(nd in n
                                                for nd in lora_name_list))
            ],
            "weight_decay":
                weight_decay,
            "lr":
                lora_lr
        },
        {
            "params": [
                p for n, p in model.named_parameters()
                if (any(nd in n
                        for nd in no_decay_name_list) and p.requires_grad)
            ],
            "weight_decay":
                0.0,
        },
    ]
    if not optimizer_grouped_parameters[1]["params"]:
        optimizer_grouped_parameters.pop(1)
    return optimizer_grouped_parameters


def print_rank_0(msg, rank=0):
    if rank <= 0:
        print(msg)


def to_device(batch, device):
    output = {}
    for k, v in batch.items():
        try:
            output[k] = v.to(device)
        except:
            output[k] = v
    return output


def set_random_seed(seed):
    if seed is not None:
        set_seed(seed)
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)


class qWenSFTDataset(Dataset):
    """
    Dataset for ChatGLMSFT model

    Args:
        dataset: dataset for supervised model
        tokenizer: tokenizer for supervised model
        max_length: max length of input
    """

    def __init__(self, dataset, processor, tokenizer, max_len=2048) -> None:
        super().__init__()
        self.processor = processor
        self.datas = dataset
        self.tokenizer = tokenizer
        self.max_len = max_len
        # self.datas = [self.create_example(data) for data in tqdm(dataset)]
        self.datas = [data for _,data in tqdm(dataset.iterrows())]

    def __len__(self):
        length = len(self.datas)
        return length

    def create_example(self, data):
        labels = []
        input_text = self.processor.apply_chat_template(data['messages'], add_generation_prompt=True)
        image = Image.open(data['local_path'])
        if not data['image_url_not_null']:
            image = image.resize((960, 1280))
        inputs = self.processor(
            image,
            input_text,
            add_special_tokens=False,
            return_tensors="pt"
        )
        answer_id = self.tokenizer(data['train_answer'], add_special_tokens=False)["input_ids"]
        answer_id.append(self.tokenizer.eos_token_id)
        labels = [-100] * inputs["input_ids"].size(1) + answer_id
        answer_tensor = torch.tensor([answer_id])
        inputs['input_ids'] = torch.cat([inputs['input_ids'], answer_tensor], dim=1)
        inputs['attention_mask'] = torch.cat([inputs['attention_mask'], torch.ones(answer_tensor.size())], dim=1)
        cross_attention_mask_add = torch.ones((1, len(answer_id), 1, inputs['pixel_values'].size(2)))
        inputs['cross_attention_mask'] = torch.cat([inputs['cross_attention_mask'], cross_attention_mask_add],
                                                   dim=1)
        inputs['labels'] = torch.tensor([labels], dtype=torch.long)
        # print(inputs['input_ids'].shape)
        return inputs

    def __getitem__(self, idx):
        data = self.create_example(self.datas[idx])
        return data

    def collate_fn(self, instances):
        input_ids, attention_mask, labels, pixel_values, aspect_ratio_ids,aspect_ratio_mask,cross_attention_mask = tuple(
            [instance[key] for instance in instances] for key in
            ("input_ids", "attention_mask", "labels", "pixel_values", "aspect_ratio_ids", "aspect_ratio_mask",
             "cross_attention_mask"))

        # print(pixel_values)
        input_ids = torch.stack(input_ids)[0]
        attention_mask = torch.stack(attention_mask)[0]
        labels = torch.stack(labels)
        pixel_values = torch.stack(pixel_values)[0] if pixel_values[0] is not None else None
        aspect_ratio_ids = torch.stack(aspect_ratio_ids)[0] if aspect_ratio_ids[0] is not None else None
        aspect_ratio_mask = torch.stack(aspect_ratio_mask)[0] if aspect_ratio_mask[0] is not None else None
        cross_attention_mask = torch.stack(cross_attention_mask)[0] if cross_attention_mask[0] is not None else None
        return dict(
            input_ids=input_ids,
            attention_mask=attention_mask,
            labels=labels,
            pixel_values=pixel_values,
            aspect_ratio_ids=aspect_ratio_ids,
            aspect_ratio_mask=aspect_ratio_mask,
            cross_attention_mask=cross_attention_mask
        )


def get_eval_ds_config(offload, dtype, stage=0):
    device = "cpu" if offload else "none"
    if dtype == "fp16":
        data_type = "fp16"
        dtype_config = {
            "enabled": True,
        }
    elif dtype == "bf16":
        data_type = "bfloat16"
        dtype_config = {"enabled": True}
    zero_opt_dict = {
        "stage": stage,
        "stage3_param_persistence_threshold": 1e4,
        "offload_param": {
            "device": device
        },
        "memory_efficient_linear": False
    }
    return {
        "train_batch_size": GLOBAL_BATCH_SIZE,
        "train_micro_batch_size_per_gpu": MICRO_BATCH_SIZE,
        "steps_per_print": 10,
        "zero_optimization": zero_opt_dict,
        data_type: dtype_config,
        "gradient_clipping": 1.0,
        "prescale_gradients": False,
        "wall_clock_breakdown": False
    }


def main():
    args = parse_args()

    print(torch.cuda.is_available())
    print(torch.cuda.device_count())

    if args.local_rank == -1:
        device = torch.device("cuda")
    else:
        torch.cuda.set_device(args.local_rank)
        device = torch.device("cuda", args.local_rank)
        # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        # torch.distributed.init_process_group(backend='nccl')
        deepspeed.init_distributed()

    args.global_rank = torch.distributed.get_rank()
    ds_config = get_train_ds_config(offload=args.offload,
                                    stage=args.zero_stage,
                                    enable_tensorboard=args.enable_tensorboard,
                                    tb_path=args.tensorboard_path,
                                    tb_name="step1_model")
    ds_config["bfloat16"]["enabled"] = True
    ds_config["fp16"]["enabled"] = False
    ds_config[
        'train_micro_batch_size_per_gpu'] = args.per_device_train_batch_size
    ds_config[
        'train_batch_size'] = args.per_device_train_batch_size * torch.distributed.get_world_size(
    ) * args.gradient_accumulation_steps
    print(ds_config)

    # If passed along, set the training seed now.
    set_random_seed(args.seed)

    torch.distributed.barrier()

    tokenizer = AutoTokenizer.from_pretrained(args.model_name_or_path, trust_remote_code=True)
    processor = AutoProcessor.from_pretrained(args.model_name_or_path)
    with open(args.train_dataset_path, 'rb') as f:
        train_data = pickle.load(f)
    print(len(train_data))
    if args.debug_code:
        train_data = train_data.sample(n=min(train_data.shape[0], 2000))
    with open(args.dev_dataset_path, 'rb') as f:
        eval_data = pickle.load(f)
    if args.debug_code:
        eval_data = eval_data.head(100)
    # print(eval_data.shape)
    # train_data = train_data.sample(frac=1., random_state=2023)
    print("eos_token:",tokenizer.eos_token_id)
    train_dataset = qWenSFTDataset(train_data, processor, tokenizer)
    eval_dataset = qWenSFTDataset(eval_data, processor, tokenizer)
    data_collator = train_dataset.collate_fn

    if args.use_4bit == 1:
        print('4bit')
        bnb_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.float16,
            bnb_4bit_use_double_quant=True,
        )
        model = MllamaForConditionalGeneration.from_pretrained(args.model_name_or_path,
                                                               torch_dtype=torch.float16,
                                                               quantization_config=bnb_config
                                                               )
    else:
        print("bf16bit")
        model = MllamaForConditionalGeneration.from_pretrained(args.model_name_or_path,
                                                               torch_dtype=torch.bfloat16,
                                                               trust_remote_code=True
                                                               )

    model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    config = LoraConfig(
        r=128,
        lora_alpha=256,
        # target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
        target_modules="all-linear",
        bias="none",
        lora_dropout=0.05,  # Conventional
        task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, config)
    model.gradient_checkpointing_enable()
    if args.lora_path != "none":
        print("load pretrain")
        d = torch.load(args.lora_path)
        final_d = {}
        for k, v in d.items():
            final_d[k] = v
        # print(final_d)
        model.load_state_dict(final_d, strict=False)
    for name, param in model.named_parameters():
        if "lora" in name:
            # print(name)
            param.requires_grad = True
    model.print_trainable_parameters()

    lora_dict = {}
    for name, param in model.named_parameters():
        if "lora" in name: lora_dict[name] = param

    # ema_engine = init_ema(args, lora_dict)

    # DataLoaders creation:
    if args.local_rank == -1:
        train_sampler = RandomSampler(train_dataset)
        eval_sampler = SequentialSampler(eval_dataset)
    else:
        train_sampler = DistributedSampler(train_dataset)
        eval_sampler = DistributedSampler(eval_dataset)

    train_dataloader = DataLoader(train_dataset,
                                  shuffle=(train_sampler is None),
                                  collate_fn=data_collator,
                                  sampler=train_sampler,
                                  num_workers=16,
                                  batch_size=args.per_device_train_batch_size,
                                  pin_memory=True)
    eval_dataloader = DataLoader(eval_dataset,
                                 shuffle=(eval_sampler is None),
                                 collate_fn=data_collator,
                                 sampler=eval_sampler,
                                 batch_size=args.per_device_eval_batch_size,
                                 pin_memory=True)
    from sklearn.metrics import log_loss

    def evaluation_dist(model, eval_dataloader):
        return 1.

    # Split weights in two groups, one with weight decay and the other not.
    optimizer_grouped_parameters = get_optimizer_grouped_parameters(
        model, args.weight_decay, args.lora_learning_rate)

    AdamOptimizer = DeepSpeedCPUAdam if args.offload else FusedAdam
    optimizer = AdamOptimizer(optimizer_grouped_parameters,
                              lr=args.learning_rate,
                              betas=(0.9, 0.95))

    num_update_steps_per_epoch = math.ceil(
        len(train_dataloader) / args.gradient_accumulation_steps)

    max_steps = math.ceil(args.num_train_epochs * num_update_steps_per_epoch)
    lr_scheduler = get_scheduler(
        name=args.lr_scheduler_type,
        optimizer=optimizer,
        num_warmup_steps=math.ceil(max_steps * 0.03) if args.num_warmup_steps == 0 else args.num_warmup_steps,
        num_training_steps=args.num_train_epochs * num_update_steps_per_epoch,
    )
    # lr_scheduler = optimization.get_constant_schedule_with_warmup(optimizer,
    #                                                               num_warmup_steps=math.ceil(max_steps * 0.1) if args.num_warmup_steps == 0 else args.num_warmup_steps)

    model, optimizer, _, lr_scheduler = deepspeed.initialize(
        model=model,
        optimizer=optimizer,
        args=args,
        config=ds_config,
        lr_scheduler=lr_scheduler,
        dist_init_required=True)

    # Train!
    print_rank_0("***** Running training *****", args.global_rank)
    print_rank_0(
        f"***** Evaluating perplexity, Epoch {0}/{args.num_train_epochs} *****",
        args.global_rank)
    # perplexity = evaluation(model, eval_dataloader)
    # print_rank_0(f"ppl: {perplexity}", args.global_rank)

    total_steps = len(train_dataloader) * args.num_train_epochs
    total_loss = 0
    best_val_loss = 1000.
    no_improve_epoch = 0.
    global_step = -1
    time_start = time.time()
    loss_fun = nn.CrossEntropyLoss()
    for epoch in range(args.num_train_epochs):
        print_rank_0(
            f"Beginning of Epoch {epoch + 1}/{args.num_train_epochs}, Total Micro Batches {len(train_dataloader)}",
            args.global_rank)
        model.train()
        for step, batch in enumerate(train_dataloader):
            global_step += 1
            batch = to_device(batch, device)
            outputs = model(**batch, return_dict=True)
            # outputs = outputs.logits
            loss = outputs.loss

            model.backward(loss)
            model.step()

            total_loss += loss.item()
            if global_step % 1 == 0:
                time_end = time.time()
                total_time = time_end - time_start  # 计算运行总时间
                time_start = time_end
                print_rank_0(
                    f"Beginning of Epoch {epoch + 1}/{args.num_train_epochs}, curr_step:{global_step}/{total_steps} curr_loss {loss.item()} lr:{lr_scheduler.get_last_lr()[0]} use time:{total_time}s",
                    args.global_rank)
            if (global_step + 1) % args.gradient_accumulation_steps == 0:
                total_loss = 0.
            if args.save_batch_steps and (global_step + 1) % args.save_batch_steps == 0:
                loss_mean = evaluation_dist(model, eval_dataloader)
                loss_mean_e = loss_mean
                # loss_mean_e = evaluation_dist(ema_engine, eval_dataloader)
                if torch.distributed.get_rank() == 0 or args.zero_stage == 3 or True:
                    print_rank_0(
                        f"***** Evaluating Loss, Epoch {epoch + 1}/{args.num_train_epochs}---{global_step}/{total_steps}*****",
                        args.global_rank)
                    print_rank_0(f"score: {loss_mean} score_:{loss_mean_e}", args.global_rank)
                if loss_mean_e <= best_val_loss:
                    print_rank_0(
                        f"val_log----epoch:{epoch},batch:{global_step + 1},save model from {best_val_loss} to {loss_mean_e} !!!",
                        args.global_rank)
                    save_model(args, model, tokenizer, f"best_val_loss_model_{global_step}")
                    best_val_loss = loss_mean_e
                    no_improve_epoch = 0
                else:
                    no_improve_epoch += 1
                    print_rank_0(
                        f"val_log----epoch:{epoch},batch:{global_step + 1},no_improve_epoch:{no_improve_epoch},curr_val_loss {loss_mean_e} best_val_loss {best_val_loss} !!!"
                        , args.global_rank)
        if args.save_per_epoch == 1:
            save_model(args, model, tokenizer, f"epoch_{epoch}_model")
        # 保存最后一轮
        if epoch == args.num_train_epochs - 1:
            loss_mean = evaluation_dist(model, eval_dataloader)
            loss_mean_e = loss_mean
            # loss_mean_e = evaluation_dist(ema_engine, eval_dataloader)
            print("last score:", loss_mean)
            print("last score_:", loss_mean_e)
            save_model(args, model, tokenizer, f"epoch_{epoch}_model")
        model.tput_timer.update_epoch_count()
        # break


def save_model(args, model, tokenizer, sub_fold=None):
    if sub_fold is not None:
        output_dir = os.path.join(args.output_dir, sub_fold)
        print_rank_0('saving model ...', args.global_rank)
        tokenizer.save_pretrained(output_dir)
        # model = convert_lora_to_linear_layer(model)
        if args.global_rank == 0:
            model_to_save = model.module if hasattr(model, 'module') else model
            model_to_save.save_pretrained(output_dir)

            # CONFIG_NAME = "config.json"
            # WEIGHTS_NAME = "adapter.bin"
            # os.makedirs(output_dir, exist_ok=True)
            # output_model_file = os.path.join(output_dir, WEIGHTS_NAME)
            # output_config_file = os.path.join(output_dir, CONFIG_NAME)
            # save_dict = model_to_save.state_dict()
            # final_d = {}
            # for k, v in save_dict.items():
            #     if "lora" in k or "score" in k:
            #         final_d[k] = v
            # torch.save(final_d, output_model_file)

        print_rank_0('saving success ...', args.global_rank)


def parse_args():
    parser = argparse.ArgumentParser(
        description=
        "Finetune a transformers model on a causal language modeling task")

    ## Tensorboard logging
    parser.add_argument('--enable_tensorboard',
                        action='store_true',
                        help='Enable tensorboard logging')
    parser.add_argument('--tensorboard_path',
                        type=str,
                        default="step1_tensorboard")

    parser.add_argument('--save_batch_steps', type=int, default=1000)
    parser.add_argument('--earystop', type=bool, default=False)
    parser.add_argument('--eary_stop_epoch', type=int, default=2)
    parser.add_argument('--save_per_epoch', type=int, default=-1)
    parser.add_argument('--use_4bit', type=int, default=0)

    parser.add_argument('--project_name', type=str, default='Coati', help="wandb project name")
    parser.add_argument('--train_dataset_path', type=str, default=None, help="train data path ")
    parser.add_argument('--dev_dataset_path', type=str, default=None, help="dev data path ")
    parser.add_argument('--max_prompt_len', type=int, default=500)
    parser.add_argument('--max_completion_len', type=int, default=500)
    parser.add_argument('--debug_code', type=int, default=0, choices=[0, 1], help="1:sample data")

    parser.add_argument('--model_name', type=str, default='llama', help="model type")
    parser.add_argument('--lora_path', type=str, default='none', help="lora path")
    parser.add_argument(
        "--model_name_or_path",
        type=str,
        help=
        "Path to pretrained model or model identifier from huggingface.co/models.",
        required=True,
    )
    parser.add_argument(
        "--per_device_train_batch_size",
        type=int,
        default=16,
        help="Batch size (per device) for the training dataloader.",
    )
    parser.add_argument(
        "--per_device_eval_batch_size",
        type=int,
        default=16,
        help="Batch size (per device) for the evaluation dataloader.",
    )
    parser.add_argument(
        "--max_seq_len",
        type=int,
        default=512,
        help="The maximum sequence length.",
    )
    parser.add_argument(
        "--learning_rate",
        type=float,
        default=1e-3,
        help=
        "Initial learning rate (after the potential warmup period) to use.",
    )
    parser.add_argument("--weight_decay",
                        type=float,
                        default=0.,
                        help="Weight decay to use.")
    parser.add_argument("--num_train_epochs",
                        type=int,
                        default=1,
                        help="Total number of training epochs to perform.")
    parser.add_argument(
        "--gradient_accumulation_steps",
        type=int,
        default=1,
        help=
        "Number of updates steps to accumulate before performing a backward/update pass.",
    )
    parser.add_argument(
        "--lr_scheduler_type",
        type=SchedulerType,
        default="cosine",
        help="The scheduler type to use.",
        choices=[
            "linear", "cosine", "cosine_with_restarts", "polynomial",
            "constant", "constant_with_warmup"
        ],
    )
    parser.add_argument(
        "--num_warmup_steps",
        type=int,
        default=0,
        help="Number of steps for the warmup in the lr scheduler.")
    parser.add_argument("--output_dir",
                        type=str,
                        default=None,
                        help="Where to store the model.")
    parser.add_argument("--seed",
                        type=int,
                        default=1234,
                        help="A seed for reproducible training.")
    parser.add_argument("--local_rank",
                        type=int,
                        default=-1,
                        help="local_rank for distributed training on gpus")
    parser.add_argument(
        "--lora_learning_rate",
        type=float,
        default=5e-4,
        help=
        "Initial LoRA learning rate (after the potential warmup period) to use."
    )
    parser.add_argument('--gradient_checkpointing',
                        action='store_true',
                        help='Enable HF gradient checkpointing for model.')
    parser.add_argument('--disable_dropout',
                        action='store_true',
                        help='Disable the dropout of the model.')
    # deepspeed features
    parser.add_argument('--offload',
                        action='store_true',
                        help='Enable ZeRO Offload techniques.')
    parser.add_argument(
        '--zero_stage',
        type=int,
        default=0,
        help='ZeRO optimization stage for Actor model (and clones).')
    ## LoRA for efficient training setting
    parser.add_argument("--lora_dim",
                        type=int,
                        default=0,
                        help="If > 0, use LoRA for efficient training.")
    parser.add_argument("--lora_config",
                        type=str,
                        default="./configs/lora_config_llama.json",
                        help="If > 0, use LoRA for efficient training.")

    parser.add_argument("--lora_module_name",
                        type=str,
                        default="decoder.layers.",
                        help="The scope of LoRA.")
    parser.add_argument('--only_optimize_lora',
                        action='store_true',
                        help='Only optimize the LoRA parameters.')
    parser = deepspeed.add_config_arguments(parser)
    args = parser.parse_args()

    # Validate settings
    if args.gradient_checkpointing and args.lora_dim > 0:
        assert (
            not args.only_optimize_lora
        ), "--gradient_checkpointing and --only_optimize_lora cannot be enabled at the same time."

    return args


if __name__ == "__main__":
    main()
