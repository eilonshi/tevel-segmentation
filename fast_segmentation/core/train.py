import os
import os.path as osp
import random
import logging
import argparse
import numpy as np
import yaml
from tabulate import tabulate

import torch
import torch.distributed as dist
import torch.cuda.amp as amp
from torch.utils.tensorboard import SummaryWriter
from torch.backends import cudnn
from typing import Tuple, List
import torch.nn as nn

from evaluate import eval_model
from fast_segmentation.core.utils import get_next_dir_name, get_next_file_name, build_model
from fast_segmentation.model_components.data_cv2 import get_data_loader
from fast_segmentation.model_components.soft_dice_loss import SoftDiceLoss
from fast_segmentation.model_components.lr_scheduler import WarmupPolyLrScheduler
from fast_segmentation.model_components.meters import TimeMeter, AvgMeter
from fast_segmentation.model_components.logger import setup_logger, print_log_msg

# fix all random seeds
torch.manual_seed(123)
torch.cuda.manual_seed(123)
np.random.seed(123)
random.seed(123)
torch.backends.cudnn.deterministic = True


def parse_args() -> argparse.Namespace:
    """
    Creates the parser for train arguments

    Returns:
        The parser
    """
    parse = argparse.ArgumentParser()

    parse.add_argument('--local_rank', dest='local_rank', type=int, default=0)
    parse.add_argument('--port', dest='port', type=int, default=44554)
    parse.add_argument('--model', dest='model', type=str, default='bisenetv2')
    parse.add_argument('--finetune-from', type=str,
                       default='/home/bina/PycharmProjects/fast-segmentation/models/5/best_model.pth')
    parse.add_argument('--im_root', type=str, default='/home/bina/PycharmProjects/fast-segmentation/data')
    parse.add_argument('--train_im_anns', type=str,
                       default='/home/bina/PycharmProjects/fast-segmentation/data/train.txt')
    parse.add_argument('--val_im_anns', type=str,
                       default='/home/bina/PycharmProjects/fast-segmentation/data/val.txt')
    parse.add_argument('--log_path', type=str,
                       default='/home/bina/PycharmProjects/fast-segmentation/logs/regular_logs')
    parse.add_argument('--false_analysis_path', type=str,
                       default='/home/bina/PycharmProjects/fast-segmentation/data/false_analysis')
    parse.add_argument('--tensorboard_path', type=str,
                       default='/home/bina/PycharmProjects/fast-segmentation/logs/tensorboard_logs')
    parse.add_argument('--models_path', type=str, default='/home/bina/PycharmProjects/fast-segmentation/models')
    parse.add_argument('--config_path', type=str,
                       default='/home/bina/PycharmProjects/fast-segmentation/configs/main_cfg.yaml')

    parse.add_argument('--amp', type=bool, default=True)

    return parse.parse_args()


def get_optimizer(net: nn.Module, lr_start, optimizer_betas, weight_decay) -> torch.optim.Optimizer:
    """
    Builds the optimizer for the given pytorch model
    Args:
        net: a pytorch nn model
        weight_decay:
        optimizer_betas:
        lr_start:

    Returns:
        an Adam optimizer for the given model
    """
    wd_params, non_wd_params = [], []

    for name, param in net.named_parameters():
        if param.dim() == 1:
            non_wd_params.append(param)
        elif param.dim() == 2 or param.dim() == 4:
            wd_params.append(param)

    params_list = [
        {'params': wd_params},
        {'params': non_wd_params, 'weight_decay': 0}
    ]
    optimizer = torch.optim.Adam(params_list, lr=lr_start, betas=optimizer_betas, weight_decay=weight_decay)

    return optimizer


def get_meters(max_iter: int, num_aux_heads: int) -> Tuple[TimeMeter, AvgMeter, AvgMeter, List[AvgMeter]]:
    """
    Creates the meters of the time and the loss

    Returns:
        tuple of - (time meter, loss meter, main loss meter, auxiliary loss meter)
    """
    time_meter = TimeMeter(max_iter)
    loss_meter = AvgMeter('loss')
    loss_pre_meter = AvgMeter('loss_prem')
    loss_aux_meters = [AvgMeter('loss_aux{}'.format(i)) for i in range(num_aux_heads)]

    return time_meter, loss_meter, loss_pre_meter, loss_aux_meters


def log_ious(writer: SummaryWriter, mious: List[float], iteration: int, headers: List[str], logger: logging.Logger,
             mode: str):
    single_scale_miou, single_scale_crop_miou, ms_flip_miou, ms_flip_crop_miou = mious

    writer.add_scalar(f"mIOU/{mode}/single_scale", single_scale_miou, iteration)
    writer.add_scalar(f"mIOU/{mode}/single_scale_crop", single_scale_crop_miou, iteration)
    writer.add_scalar(f"mIOU/{mode}/multi_scale_flip", ms_flip_miou, iteration)
    writer.add_scalar(f"mIOU/{mode}/multi_scale_flip_crop", ms_flip_crop_miou, iteration)

    logger.info(tabulate([mious, ], headers=headers, tablefmt='orgtbl'))


def save_best_model(cur_score: float, best_score: float, models_dir: str, net: nn.Module) -> float:
    """
    Saves the model if it is better than the last best model, and returns the score of the current best model

    Args:
        cur_score: the score of the current model
        best_score: the score of the last best model
        models_dir: the path of the directory of the model weights to save the weights of the best model
        net: the current pytorch model

    Returns:
        the best score
    """
    if cur_score > best_score:
        best_score = cur_score
        save_pth = os.path.join(models_dir, f"best_model.pth")
        state = net.module.state_dict()

        if dist.get_rank() == 0:
            torch.save(state, save_pth)

    return best_score


def save_evaluation_log(models_dir: str, logger: logging.Logger, net: nn.Module, writer: SummaryWriter, iteration: int,
                        best_score: float, ims_per_gpu: int, crop_size: Tuple[int, int], log_path: str, im_root: str,
                        val_im_anns: str, false_analysis_path: str, train_im_anns: str) -> float:
    """
    Saves a log with the SummaryWriter, and if the model is the best model until now, saves the model as the best model

    Args:
        train_im_anns:
        false_analysis_path:
        val_im_anns:
        im_root:
        log_path:
        models_dir: path to the directory to save the model in, in case it is the best model
        logger: the logger that logs the evaluation log
        net: the pytorch network
        writer: the tensorboard summary writer
        iteration: the index of the current iteration
        best_score: the score of the best model until now
        crop_size:
        ims_per_gpu:

    Returns:
        the score of the best model
    """
    log_pth = get_next_file_name(log_path, prefix='model_final_', suffix='.pth')
    logger.info(f'\nevaluating the model \nsave models to {log_pth}')

    torch.cuda.empty_cache()

    # evaluate val set
    heads_val, mious_val = eval_model(net=net, ims_per_gpu=ims_per_gpu, im_root=im_root,
                                      im_anns=val_im_anns, crop_size=crop_size,
                                      false_analysis_path=false_analysis_path)
    log_ious(writer, mious_val, iteration, heads_val, logger, mode='val')

    # evaluate train set
    heads_train, mious_train = eval_model(net=net, ims_per_gpu=ims_per_gpu, im_root=im_root,
                                          im_anns=train_im_anns, crop_size=crop_size,
                                          false_analysis_path=false_analysis_path)
    log_ious(writer, mious_train, iteration, heads_train, logger, mode='train')

    # save best model
    best_score = save_best_model(mious_val[0], best_score, models_dir, net)

    return best_score


def save_checkpoint(models_dir: str, net: nn.Module):
    """
    Saves a checkpoint of the given network to the given directory

    Args:
        models_dir: the path to the directory to save the model in
        net: a pytorch network

    Returns:
        None
    """
    save_pth = get_next_file_name(models_dir, prefix='model_final_', suffix='.pth')
    state = net.module.state_dict()

    if dist.get_rank() == 0:
        torch.save(state, save_pth)


def train(ims_per_gpu: int, scales: Tuple, crop_size: Tuple[int, int], max_iter: int, use_sync_bn: bool,
          num_aux_heads: int, warmup_iters: int, use_fp16: bool, message_iters: int, checkpoint_iters: int,
          lr_start: float, optimizer_betas: Tuple[float, float], weight_decay: float, log_path: str, im_root: str,
          val_im_anns: str, false_analysis_path: str, train_im_anns: str):
    """
    The main function for training the semantic segmentation model

    Args:
        train_im_anns:
        false_analysis_path:
        val_im_anns:
        im_root:
        log_path:
        weight_decay:
        optimizer_betas:
        lr_start:
        ims_per_gpu:
        scales:
        num_aux_heads:
        warmup_iters:
        use_fp16:
        message_iters:
        checkpoint_iters:
        use_sync_bn:
        max_iter:
        crop_size:

    Returns:
        None
    """
    logger = logging.getLogger()
    tensorboard_log_dir = get_next_dir_name(root_dir=args.tensorboard_path)
    models_dir = get_next_dir_name(root_dir=args.models_path)
    writer = SummaryWriter(log_dir=tensorboard_log_dir)
    is_dist = dist.is_initialized()

    # set all components
    data_loader = get_data_loader(data_path=args.im_root, ann_path=args.train_im_anns, ims_per_gpu=ims_per_gpu,
                                  scales=scales, crop_size=crop_size, max_iter=max_iter, mode='train',
                                  distributed=is_dist)
    net = build_model(args.model, is_train=True, is_distributed=is_dist, pretrained_model_path=args.finetune_from,
                      use_sync_bn=use_sync_bn)
    criteria_pre = SoftDiceLoss()
    criteria_aux = [SoftDiceLoss() for _ in range(num_aux_heads)]
    optimizer = get_optimizer(net=net, lr_start=lr_start, optimizer_betas=optimizer_betas, weight_decay=weight_decay)
    scaler = amp.GradScaler()  # mixed precision training
    time_meter, loss_meter, loss_pre_meter, loss_aux_meters = get_meters(max_iter=max_iter, num_aux_heads=num_aux_heads)
    lr_scheduler = WarmupPolyLrScheduler(optimizer, power=0.9, max_iter_=max_iter, warmup_iter=warmup_iters,
                                         warmup_ratio=0.1, warmup='exp', last_epoch=-1, )
    best_score = 0

    # train loop
    for iteration, (image, label) in enumerate(data_loader):
        image = image.cuda()
        label = label.cuda()

        if iteration == 0:
            writer.add_graph(net, image)

        label = torch.squeeze(label, 1)
        optimizer.zero_grad()

        with amp.autocast(enabled=use_fp16):  # get main loss and auxiliary losses
            logits, *logits_aux = net(image)
            loss_pre = criteria_pre(logits, label)
            loss_aux = [criteria(logits, label) for criteria, logits in zip(criteria_aux, logits_aux)]

            loss = loss_pre + sum(loss_aux)
            writer.add_scalar("Loss/train", loss, iteration)

        scaler.scale(loss).backward()
        scaler.step(optimizer)
        scaler.update()
        torch.cuda.synchronize()

        time_meter.update()
        loss_meter.update(loss.item())
        loss_pre_meter.update(loss_pre.item())
        _ = [metric.update(loss.item()) for metric, loss in zip(loss_aux_meters, loss_aux)]

        # print training log message
        if (iteration + 1) % message_iters == 0:
            lr = lr_scheduler.get_lr()
            lr = sum(lr) / len(lr)
            print_log_msg(iteration, max_iter, lr, time_meter, loss_meter, loss_pre_meter, loss_aux_meters)

        # saving the model and evaluating it
        if (iteration + 1) % checkpoint_iters == 0:
            best_score = save_evaluation_log(models_dir, logger, net, writer, iteration, best_score,
                                             ims_per_gpu=ims_per_gpu, crop_size=crop_size, log_path=log_path,
                                             im_root=im_root, val_im_anns=val_im_anns,
                                             false_analysis_path=false_analysis_path, train_im_anns=train_im_anns)
            save_checkpoint(models_dir, net)

        lr_scheduler.step()

    writer.flush()
    writer.close()


if __name__ == "__main__":

    args = parse_args()

    with open(args.config_path) as f:
        cfg = yaml.load(f, Loader=yaml.FullLoader)

    torch.cuda.empty_cache()
    torch.cuda.set_device(args.local_rank)
    dist.init_process_group(
        backend='nccl',
        init_method='tcp://127.0.0.1:{}'.format(args.port),
        world_size=torch.cuda.device_count(),
        rank=args.local_rank
    )

    if not osp.exists(args.log_path):
        os.makedirs(args.log_path)

    setup_logger('{}-train'.format(args.model), args.log_path)

    train(ims_per_gpu=cfg['ims_per_gpu'], scales=cfg['scales'], crop_size=cfg['crop_size'], max_iter=cfg['max_iter'],
          use_sync_bn=cfg['use_sync_bn'], num_aux_heads=cfg['num_aux_heads'], warmup_iters=cfg['warmup_iters'],
          use_fp16=cfg['use_fp16'], message_iters=cfg['message_iters'], checkpoint_iters=cfg['checkpoint_iters'],
          lr_start=cfg['lr_start'], optimizer_betas=cfg['optimizer_betas'], weight_decay=cfg['weight_decay'],
          log_path=args.log_path, im_root=args.im_root, val_im_anns=args.val_im_anns,
          false_analysis_path=args.false_analysis_path, train_im_anns=args.train_im_anns)
