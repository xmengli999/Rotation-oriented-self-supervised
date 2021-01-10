import argparse
import os
import sys
import shutil
import time

import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.optim
import torch.utils.data
import torch.utils.data.distributed
import torchvision.transforms as transforms

import datasets
import models
import math
import random
from lib.NCEAverage import NCEAverage
from lib.LinearAverage import LinearAverage
from lib.NCECriterion import NCECriterion
from lib.BatchAverage import BatchCriterion
from lib.BatchAverageRot import BatchCriterionRot
from lib.utils import AverageMeter
from test import kNN
import numpy as np

from lib.utils import save_checkpoint, adjust_learning_rate, accuracy
from lib.utils import get_color_distortion, gaussian_blur
from tensorboardX import SummaryWriter

model_names = sorted(name for name in models.__dict__
    if name.islower() and not name.startswith("__")
    and callable(models.__dict__[name]))


parser = argparse.ArgumentParser(description='PyTorch ImageNet Training')
parser.add_argument('data', metavar='DIR',
                    help='path to dataset')
parser.add_argument('--arch', '-a', metavar='ARCH', default='resnet18',
                    choices=model_names,
                    help='model architecture: ' +
                        ' | '.join(model_names) +
                        ' (default: resnet18)')
parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
                    help='number of data loading workers (default: 4)')
parser.add_argument('--epochs', default=300, type=int, metavar='N',
                    help='number of total epochs to run')
parser.add_argument('--start-epoch', default=0, type=int, metavar='N',
                    help='manual epoch number (useful on restarts)')
parser.add_argument('-b', '--batch-size', default=256, type=int,
                    metavar='N', help='mini-batch size (default: 256)')
parser.add_argument('--lr', '--learning-rate', default=0.03, type=float,
                    metavar='LR', help='initial learning rate')
parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                    help='momentum')
parser.add_argument('--weight-decay', '--wd', default=1e-4, type=float,
                    metavar='W', help='weight decay (default: 1e-4)')
parser.add_argument('--print-freq', '-p', default=10, type=int,
                    metavar='N', help='print frequency (default: 10)')
parser.add_argument('--resume', default='', type=str, metavar='PATH',
                    help='path to latest checkpoint (default: none)')
parser.add_argument('--test-only', action='store_true', help='test only')
parser.add_argument('-e', '--evaluate', dest='evaluate', action='store_true',
                    help='evaluate model on validation set')
parser.add_argument('--low-dim', default=128, type=int,
                    metavar='D', help='feature dimension')
parser.add_argument('--nce-k', default=4096, type=int,
                    metavar='K', help='number of negative samples for NCE')
parser.add_argument('--nce-t', default=0.07, type=float,
                    metavar='T', help='temperature parameter for softmax')
parser.add_argument('--nce-m', default=0.5, type=float,
                    help='momentum for non-parametric updates')
parser.add_argument('--iter_size', default=1, type=int,
                    help='caffe style iter size')

parser.add_argument('--result', default="", type=str)
parser.add_argument('--seedstart', default=0, type=int)
parser.add_argument('--seed', default=0, type=int)
parser.add_argument('--seedend', default=5, type=int)


parser.add_argument("--synthesis", action="store_true")
parser.add_argument('--showfeature', action="store_true")
parser.add_argument('--multiaug', action="store_true")
parser.add_argument('--multitask', action="store_true")
parser.add_argument("--multitaskposrot", action="store_true")
parser.add_argument('--domain', action="store_true")
parser.add_argument("--saveembed", type=str, default="")

best_prec1 = 0

def main():

    global args, best_prec1
    args = parser.parse_args()

    my_whole_seed = 111
    random.seed(my_whole_seed)
    np.random.seed(my_whole_seed)
    torch.manual_seed(my_whole_seed)
    torch.cuda.manual_seed_all(my_whole_seed)
    torch.cuda.manual_seed(my_whole_seed)
    np.random.seed(my_whole_seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    os.environ['PYTHONHASHSEED'] = str(my_whole_seed)

    for kk_time in range(args.seedstart, args.seedend):
        args.seed = kk_time
        args.result = args.result + str(args.seed)

        # create model
        model = models.__dict__[args.arch](low_dim=args.low_dim, multitask=args.multitask , showfeature=args.showfeature, args = args)
        #
        # from models.Gresnet import ResNet18
        # model = ResNet18(low_dim=args.low_dim, multitask=args.multitask)
        model = torch.nn.DataParallel(model).cuda()

        # Data loading code
        normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                         std=[0.229, 0.224, 0.225])

        aug = transforms.Compose([transforms.RandomResizedCrop(224, scale=(0.2, 1.)),
                                  transforms.RandomGrayscale(p=0.2),
                                  transforms.ColorJitter(0.4, 0.4, 0.4, 0.4),
                                  transforms.RandomHorizontalFlip(),
                                  transforms.ToTensor(),
                                  normalize])
        # aug = transforms.Compose([transforms.RandomResizedCrop(224, scale=(0.08, 1.), ratio=(3 / 4, 4 / 3)),
        #                           transforms.RandomHorizontalFlip(p=0.5),
        #                           get_color_distortion(s=1),
        #                           transforms.Lambda(lambda x: gaussian_blur(x)),
        #                           transforms.ToTensor(),
        #                           normalize])
        # aug = transforms.Compose([transforms.RandomRotation(60),
        #                           transforms.RandomResizedCrop(224, scale=(0.6, 1.)),
        #                           transforms.RandomGrayscale(p=0.2),
        #                           transforms.ColorJitter(0.4, 0.4, 0.4, 0.4),
        #                           transforms.RandomHorizontalFlip(),
        #                           transforms.ToTensor(),
        #                             normalize])
        aug_test = transforms.Compose([
                transforms.Resize(224),
                transforms.ToTensor(),
                normalize])

        # dataset
        import datasets.fundus_kaggle_dr as medicaldata
        train_dataset = medicaldata.traindataset(root=args.data, transform=aug, train=True, args=args)
        train_loader = torch.utils.data.DataLoader(
            train_dataset, batch_size=args.batch_size, shuffle=True, pin_memory=True, num_workers=8, drop_last=True if args.multiaug else False,  worker_init_fn=random.seed(my_whole_seed))


        valid_dataset = medicaldata.traindataset(root=args.data, transform=aug_test, train=False, test_type="amd", args=args)
        val_loader = torch.utils.data.DataLoader(
            valid_dataset, batch_size=args.batch_size, shuffle=False, pin_memory=True, num_workers=8, worker_init_fn=random.seed(my_whole_seed))
        valid_dataset_gon = medicaldata.traindataset(root=args.data, transform=aug_test, train=False, test_type="gon",
                                                 args=args)
        val_loader_gon = torch.utils.data.DataLoader(
            valid_dataset_gon, batch_size=args.batch_size, shuffle=False, pin_memory=True, num_workers=8,
            worker_init_fn=random.seed(my_whole_seed))
        valid_dataset_pm = medicaldata.traindataset(root=args.data, transform=aug_test, train=False, test_type="pm",
                                                 args=args)
        val_loader_pm = torch.utils.data.DataLoader(
            valid_dataset_pm, batch_size=args.batch_size, shuffle=False, pin_memory=True, num_workers=8,
            worker_init_fn=random.seed(my_whole_seed))



        # define lemniscate and loss function (criterion)
        ndata = train_dataset.__len__()

        lemniscate = LinearAverage(args.low_dim, ndata, args.nce_t, args.nce_m).cuda()
        local_lemniscate = None

        if args.multitaskposrot:
            print ("running multi task with positive")
            criterion = BatchCriterionRot(1, 0.1, args.batch_size, args).cuda()
        elif args.domain:
            print ("running domain with four types--unify ")
            from lib.BatchAverageFour import BatchCriterionFour
            # criterion = BatchCriterionTriple(1, 0.1, args.batch_size, args).cuda()
            criterion = BatchCriterionFour(1, 0.1, args.batch_size, args).cuda()
        elif args.multiaug:
            print ("running multi task")
            criterion = BatchCriterion(1, 0.1, args.batch_size, args).cuda()
        else:
            criterion = nn.CrossEntropyLoss().cuda()


        if args.multitask:
            cls_criterion = nn.CrossEntropyLoss().cuda()
        else:
            cls_criterion = None

        optimizer = torch.optim.Adam(model.parameters(), args.lr,
                                     weight_decay=args.weight_decay)

        # optionally resume from a checkpoint
        if args.resume:
            if os.path.isfile(args.resume):
                print("=> loading checkpoint '{}'".format(args.resume))
                checkpoint = torch.load(args.resume)
                args.start_epoch = checkpoint['epoch']
                model.load_state_dict(checkpoint['state_dict'])
                lemniscate = checkpoint['lemniscate']
                optimizer.load_state_dict(checkpoint['optimizer'])
                print("=> loaded checkpoint '{}' (epoch {})"
                      .format(args.resume, checkpoint['epoch']))
            else:
                print("=> no checkpoint found at '{}'".format(args.resume))


        if args.evaluate:
            knn_num = 100
            auc, acc, precision, recall, f1score = kNN(args, model, lemniscate, train_loader, val_loader, knn_num, args.nce_t, 2)
            return



        # mkdir result folder and tensorboard
        os.makedirs(args.result, exist_ok=True)
        writer = SummaryWriter("runs/" + str(args.result.split("/")[-1]))
        writer.add_text('Text', str(args))

        # copy code
        import shutil, glob
        source = glob.glob("*.py")
        source += glob.glob("*/*.py")
        os.makedirs(args.result + "/code_file", exist_ok=True)
        for file in source:
            name = file.split("/")[0]
            if name == file:
                shutil.copy(file, args.result + "/code_file/")
            else:
                os.makedirs(args.result + "/code_file/" + name, exist_ok=True)
                shutil.copy(file, args.result + "/code_file/" + name)

        for epoch in range(args.start_epoch, args.epochs):
            lr = adjust_learning_rate(optimizer, epoch, args, [100, 200])
            writer.add_scalar("lr", lr, epoch)

            # # train for one epoch
            loss = train(train_loader, model, lemniscate, local_lemniscate, criterion, cls_criterion, optimizer, epoch, writer)
            writer.add_scalar("train_loss", loss, epoch)

            # gap_int = 10
            # if (epoch) % gap_int == 0:
            #     knn_num = 100
            #     auc, acc, precision, recall, f1score = kNN(args, model, lemniscate, train_loader, val_loader, knn_num, args.nce_t, 2)
            #     writer.add_scalar("test_auc", auc, epoch)
            #     writer.add_scalar("test_acc", acc, epoch)
            #     writer.add_scalar("test_precision", precision, epoch)
            #     writer.add_scalar("test_recall", recall, epoch)
            #     writer.add_scalar("test_f1score", f1score, epoch)
            #
            #     auc, acc, precision, recall, f1score = kNN(args, model, lemniscate, train_loader, val_loader_gon,
            #                                                knn_num, args.nce_t, 2)
            #     writer.add_scalar("gon/test_auc", auc, epoch)
            #     writer.add_scalar("gon/test_acc", acc, epoch)
            #     writer.add_scalar("gon/test_precision", precision, epoch)
            #     writer.add_scalar("gon/test_recall", recall, epoch)
            #     writer.add_scalar("gon/test_f1score", f1score, epoch)
            #     auc, acc, precision, recall, f1score = kNN(args, model, lemniscate, train_loader, val_loader_pm,
            #                                                knn_num, args.nce_t, 2)
            #     writer.add_scalar("pm/test_auc", auc, epoch)
            #     writer.add_scalar("pm/test_acc", acc, epoch)
            #     writer.add_scalar("pm/test_precision", precision, epoch)
            #     writer.add_scalar("pm/test_recall", recall, epoch)
            #     writer.add_scalar("pm/test_f1score", f1score, epoch)

                # save checkpoint
            save_checkpoint({
                'epoch': epoch,
                'arch': args.arch,
                'state_dict': model.state_dict(),
                'lemniscate': lemniscate,
                'optimizer': optimizer.state_dict(),
            }, filename=args.result + "/fold" + str(args.seedstart) + "-epoch-" + str(epoch) + ".pth.tar")


def train(train_loader, model, lemniscate, local_lemniscate, criterion, cls_criterion, optimizer, epoch, writer):
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()

    losses_ins = AverageMeter()
    losses_rot = AverageMeter()

    # switch to train mode
    model.train()

    end = time.time()
    optimizer.zero_grad()

    for i, (input, target, index, name) in enumerate(train_loader):
        # measure data loading time
        data_time.update(time.time() - end)

        # compute output
        if args.multitask:
            input = torch.cat(input, 0).cuda()
            index = torch.cat([index, index], 0).cuda()
            rotation_label = torch.cat([target[1], target[1]], 0).cuda()

            # initialize tensors
            tensors = {}
            tensors['dataX'] = torch.FloatTensor()
            tensors['index'] = torch.LongTensor()
            tensors['index_index'] = torch.LongTensor()
            tensors['labels'] = torch.LongTensor()


            # construct rotated input
            tensors['dataX'].resize_(input.size()).copy_(input)
            dataX_90 = torch.flip(torch.transpose(input, 2, 3), [2])
            dataX_180 = torch.flip(torch.flip(input, [2]), [3])
            dataX_270 = torch.transpose(torch.flip(input, [2]), 2, 3)
            dataX = torch.stack([input, dataX_90, dataX_180, dataX_270], dim=1)
            batch_size, rotations, channels, height, width = dataX.size()
            dataX = dataX.view([batch_size * rotations, channels, height, width])

            # construct rotated label and index
            rotation_label = torch.stack([rotation_label, torch.ones_like(rotation_label), 2*torch.ones_like(rotation_label), 3*torch.ones_like(rotation_label)], dim=1)
            rotation_label = rotation_label.view([batch_size*rotations])
            index = torch.stack([index, index, index, index], dim=1)
            index = index.view([batch_size * rotations])


            feature, pred_rot, feture_whole = model(dataX)

            loss_instance = criterion(feature, index) / args.iter_size
            loss_cls = cls_criterion(pred_rot, rotation_label)
            loss =  loss_instance + 1.0 * loss_cls

            losses_ins.update(loss_instance.item() * args.iter_size, input.size(0))
            losses_rot.update(loss_cls.item() * args.iter_size, input.size(0))

        elif args.multiaug:

            if args.domain:
                dataX = torch.cat(input, 0).cuda()
                ori_data = dataX[:int(dataX.shape[0] / 2)]
                syn_data = dataX[int(dataX.shape[0] / 2):]
                data = [ori_data, syn_data]
                dataX = torch.stack(data, dim=1).cuda()
                batch_size, types, channels, height, width = dataX.size()
                input = dataX.view([batch_size * types, channels, height, width])

            # input = torch.cat(input, 0).cuda()
            feature = model(input)
            loss = criterion(feature, index) / args.iter_size
        else:
            input = input.cuda()
            index = index.cuda()

            feature = model(input)
            output = lemniscate(feature, index)
            loss = criterion(output, index) / args.iter_size

        loss.backward()

        # measure accuracy and record loss
        losses.update(loss.item() * args.iter_size, input.size(0))

        if (i+1) % args.iter_size == 0:
            # compute gradient and do SGD step
            optimizer.step()
            optimizer.zero_grad()

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            print('Epoch: [{0}][{1}/{2}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'.format(
                   epoch, i, len(train_loader), batch_time=batch_time,
                   data_time=data_time, loss=losses))

    writer.add_scalar("losses_ins", losses_ins.avg, epoch)
    writer.add_scalar("losses_rot", losses_rot.avg, epoch)

    return losses.avg



if __name__ == '__main__':
    main()
