from __future__ import print_function

import logging
import os
import sys

import torch

import args

torch.set_printoptions(profile="full")
import torch.nn as nn
import torch.nn.functional as F
import torch.backends.cudnn as cudnn
import torch.optim as optim
import torch.utils.data
import torchvision.datasets as dset
import torchvision.transforms as transforms
import torchvision.utils as vutils
from torch.autograd import Variable
import numpy as np
import matplotlib

matplotlib.use('Agg')  # For error: _tkinter.TclError: couldn't connect to display "localhost:10.0"
import matplotlib.pyplot as plt
import pickle

sys.path.append('models')
import resnet
import noisy_folder

opt = args.args()

base_dir = '{}/{}_{}_{}_{}'.format(opt.save_dir, opt.dataset, opt.model, opt.noise_type, int(opt.noise * 100))

if opt.load_dir:
    assert os.path.isdir(opt.load_dir)
    opt.save_dir = opt.load_dir
else:
    opt.save_dir = '{}_PL_cut{}'.format(base_dir, int(opt.cut * 100))
opt.pretrained = '{}/checkpoint_epoch1439.pth.tar'.format(base_dir)
try:
    os.makedirs(opt.save_dir)
except OSError:
    pass
cudnn.benchmark = True

logger = logging.getLogger("ydk_logger")
fileHandler = logging.FileHandler(opt.save_dir + '/train.log')
streamHandler = logging.StreamHandler()

logger.addHandler(fileHandler)
logger.addHandler(streamHandler)

logger.setLevel(logging.INFO)
logger.info(opt)
###################################################################################################
if opt.dataset == 'cifar10_wo_val':
    num_classes = 10;
    in_channels = 3
else:
    logger.info('There exists no data')

##
# Computing mean
trainset = dset.ImageFolder(root='{}/{}/train'.format(opt.dataroot, opt.dataset),
                            transform=transforms.ToTensor())
trainloader = torch.utils.data.DataLoader(trainset, batch_size=opt.batchSize,
                                          shuffle=False, num_workers=opt.workers)
mean = 0
for i, data in enumerate(trainloader, 0):
    imgs, labels = data
    mean += torch.from_numpy(np.mean(np.asarray(imgs), axis=(2, 3))).sum(0)
mean = mean / len(trainset)
##

transform_train = transforms.Compose(
    [
        transforms.Resize(opt.imageSize),
        transforms.RandomCrop(opt.imageSize, padding=4),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        transforms.Normalize((mean[0], mean[1], mean[2]), (1.0, 1.0, 1.0))
    ])

transform_test = transforms.Compose(
    [
        transforms.Resize(opt.imageSize),
        transforms.ToTensor(),
        transforms.Normalize((mean[0], mean[1], mean[2]), (1.0, 1.0, 1.0))
    ])

logger.info(transform_train)
logger.info(transform_test)

with open('noise/%s/train_labels_n%02d_%s' % (opt.noise_type, opt.noise * 000, opt.dataset), 'rb') as fp:
    clean_labels = pickle.load(fp)
with open('noise/%s/train_labels_n%02d_%s' % (opt.noise_type, opt.noise * 100, opt.dataset), 'rb') as fp:
    noisy_labels = pickle.load(fp)

trainset = noisy_folder.ImageFolder(root='{}/{}/train'.format(opt.dataroot, opt.dataset),
                                    noisy_labels=noisy_labels, transform=transform_train)
testset = dset.ImageFolder(root='{}/{}/test'.format(opt.dataroot, opt.dataset),
                           transform=transform_test)

clean_labels = list(clean_labels.astype(int))
noisy_labels = list(noisy_labels.astype(int))

inds_noisy = np.asarray([ind for ind in range(len(trainset)) if trainset.imgs[ind][-1] != clean_labels[ind]])
inds_clean = np.delete(np.arange(len(trainset)), inds_noisy)

trainloader = torch.utils.data.DataLoader(trainset, batch_size=opt.batchSize,
                                          shuffle=True, num_workers=opt.workers)
testloader = torch.utils.data.DataLoader(testset, batch_size=opt.batchSize,
                                         shuffle=False, num_workers=opt.workers)

if opt.model == 'resnet34':
    net = resnet.resnet34(in_channels=in_channels, num_classes=num_classes)
else:
    logger.info('no model exists')

weight = torch.FloatTensor(num_classes).zero_() + 1.
for i in range(num_classes):
    weight[i] = (torch.from_numpy(np.array(trainset.imgs)[:, 1].astype(int)) == i).sum()
weight = 1 / (weight / weight.max())

criterion = nn.CrossEntropyLoss(weight=weight)
criterion_nll = nn.NLLLoss()
criterion_nr = nn.CrossEntropyLoss(reduce=False)

net.cuda()
criterion.cuda()
criterion_nll.cuda()
criterion_nr.cuda()

optimizer = optim.SGD(net.parameters(),
                      lr=opt.lr, momentum=opt.momentum, weight_decay=opt.weight_decay)

train_preds = torch.zeros(len(trainset), num_classes) - 1.
num_hist = 10
train_preds_hist = torch.zeros(len(trainset), num_hist, num_classes)
pl_ratio = 0.;
nl_ratio = 1. - pl_ratio
train_losses = torch.zeros(len(trainset)) - 1.

##
# output: pretrained net & optimizer, train_preds_hist
ckpt = torch.load(opt.pretrained)
net.load_state_dict(ckpt['state_dict'])
optimizer.load_state_dict(ckpt['optimizer'])
train_preds_hist = ckpt['train_preds_hist']
##

if opt.load_dir:
    ckpt = torch.load(opt.load_dir + '/' + opt.load_pth)
    net.load_state_dict(ckpt['state_dict'])
    optimizer.load_state_dict(ckpt['optimizer'])
    train_preds_hist = ckpt['train_preds_hist']
    pl_ratio = ckpt['pl_ratio']
    nl_ratio = ckpt['nl_ratio']
    epoch_resume = ckpt['epoch']
    logger.info('loading network SUCCESSFUL')
else:
    epoch_resume = 0
    logger.info('loading network FAILURE')
###################################################################################################
# Start training

best_test_acc = 0.0
for epoch in range(epoch_resume, opt.max_epochs):
    train_loss = train_loss_neg = train_acc = 0.0
    pl = 0.;
    nl = 0.;
    if epoch in opt.epoch_step:
        for param_group in optimizer.param_groups:
            param_group['lr'] *= 0.1
            opt.lr = param_group['lr']

    for i, data in enumerate(trainloader, 0):
        net.zero_grad()
        imgs, labels, index = data
        labels_neg = (labels.unsqueeze(-1).repeat(1, opt.ln_neg)
                      + torch.LongTensor(len(labels), opt.ln_neg).random_(1, num_classes)) % num_classes

        assert labels_neg.max() <= num_classes - 1
        assert labels_neg.min() >= 0
        assert (labels_neg != labels.unsqueeze(-1).repeat(1, opt.ln_neg)).sum() == len(labels) * opt.ln_neg

        imgs = Variable(imgs.cuda());
        labels = Variable(labels.cuda());
        labels_neg = Variable(labels_neg.cuda())

        logits = net(imgs)

        ##
        s_neg = torch.log(torch.clamp(1. - F.softmax(logits, -1), min=1e-5, max=1.))
        s_neg *= weight[labels].unsqueeze(-1).expand(s_neg.size()).cuda()

        _, pred = torch.max(logits.data, -1)
        acc = float((pred == labels.data).sum())
        train_acc += acc

        train_loss += imgs.size(0) * criterion(logits, labels).data
        train_loss_neg += imgs.size(0) * criterion_nll(s_neg, labels_neg[:, 0]).data
        train_losses[index] = criterion_nr(logits, labels).cpu().data
        ##

        labels[train_preds_hist.mean(1)[index, labels] < opt.cut] = -100
        labels_neg = labels_neg * 0 - 100

        loss = criterion(logits, labels) * float((labels >= 0).sum())
        loss_neg = criterion_nll(s_neg.repeat(opt.ln_neg, 1), labels_neg.t().contiguous().view(-1)) * float(
            (labels_neg >= 0).sum())

        ((loss + loss_neg) / (float((labels >= 0).sum()) + float((labels_neg[:, 0] >= 0).sum()))).backward()
        optimizer.step()

        train_preds[index.cpu()] = F.softmax(logits, -1).cpu().data
        pl += float((labels >= 0).sum())
        nl += float((labels_neg[:, 0] >= 0).sum())

    train_loss /= len(trainset)
    train_loss_neg /= len(trainset)
    train_acc /= len(trainset)
    pl_ratio = pl / float(len(trainset))
    nl_ratio = nl / float(len(trainset))
    noise_ratio = 1. - pl_ratio

    noise = (np.array(trainset.imgs)[:, 1].astype(int) != np.array(clean_labels)).sum()
    logger.info('[%6d/%6d] loss: %5f, loss_neg: %5f, acc: %5f, lr: %5f, noise: %d, pl: %5f, nl: %5f, noise_ratio: %5f'
                % (epoch, opt.max_epochs, train_loss, train_loss_neg, train_acc, opt.lr, noise, pl_ratio, nl_ratio,
                   noise_ratio))
    ###############################################################################################
    if epoch == 0:
        for i in range(in_channels): imgs.data[:, i] += mean[i].cuda()
        img = vutils.make_grid(imgs.data)
        vutils.save_image(img, '%s/x.jpg' % (opt.save_dir))
        logger.info('%s/x.jpg saved' % (opt.save_dir))

    net.eval()
    test_loss = test_acc = 0.0
    with torch.no_grad():
        for i, data in enumerate(testloader, 0):
            imgs, labels = data
            imgs = Variable(imgs.cuda());
            labels = Variable(labels.cuda())

            logits = net(imgs)
            loss = criterion(logits, labels)
            test_loss += imgs.size(0) * loss.data

            _, pred = torch.max(logits.data, -1)
            acc = float((pred == labels.data).sum())
            test_acc += acc

    test_loss /= len(testset)
    test_acc /= len(testset)

    inds = np.argsort(np.array(train_losses))[::-1]
    rnge = int(len(trainset) * noise_ratio)
    inds_filt = inds[:rnge]
    recall = float(len(np.intersect1d(inds_filt, inds_noisy))) / float(len(inds_noisy))
    precision = float(len(np.intersect1d(inds_filt, inds_noisy))) / float(rnge)
    ###############################################################################################
    logger.info('\tTESTING...loss: %5f, acc: %5f, best_acc: %5f, recall: %5f, precision: %5f'
                % (test_loss, test_acc, best_test_acc, recall, precision))
    net.train()
    ###############################################################################################
    assert train_preds[train_preds < 0].nelement() == 0
    train_preds_hist[:, epoch % num_hist] = train_preds
    train_preds = train_preds * 0 - 1.
    assert train_losses[train_losses < 0].nelement() == 0
    train_losses = train_losses * 0 - 1.
    ###############################################################################################
    is_best = test_acc > best_test_acc
    best_test_acc = max(test_acc, best_test_acc)
    state = ({
        'epoch': epoch,
        'state_dict': net.state_dict(),
        'optimizer': optimizer.state_dict(),
        'train_preds_hist': train_preds_hist,
        'pl_ratio': pl_ratio,
        'nl_ratio': nl_ratio,
    })
    logger.info('saving model...')
    fn = os.path.join(opt.save_dir, 'checkpoint.pth.tar')
    torch.save(state, fn)
    if epoch % 100 == 0 or epoch == opt.max_epochs - 1:
        fn = os.path.join(opt.save_dir, 'checkpoint_epoch%d.pth.tar' % (epoch))
        torch.save(state, fn)
    # if is_best:
    # 	logger.info('saving model...')
    # 	fn = os.path.join(opt.save_dir, 'checkpoint.pth.tar')
    # 	torch.save(state, fn)
    # 	fn_best = os.path.join(opt.save_dir, 'model_best.pth.tar')
    # 	logger.info('saving best model...')
    # 	shutil.copyfile(fn, fn_best)

    if epoch % 10 == 0:
        logger.info('saving histogram...')
        plt.hist(train_preds_hist.mean(1)[torch.arange(len(trainset)), np.array(trainset.imgs)[:, 1].astype(int)],
                 bins=20, range=(0., 1.), edgecolor='black', color='g')
        plt.xlabel('probability');
        plt.ylabel('number of data')
        plt.grid()
        plt.savefig(opt.save_dir + '/histogram_epoch%03d.jpg' % (epoch))
        plt.clf()

        logger.info('saving separated histogram...')
        plt.hist(train_preds_hist.mean(1)[
                     torch.arange(len(trainset))[inds_clean], np.array(trainset.imgs)[:, 1].astype(int)[inds_clean]],
                 bins=20, range=(0., 1.), edgecolor='black', alpha=0.5, label='clean')
        plt.hist(train_preds_hist.mean(1)[
                     torch.arange(len(trainset))[inds_noisy], np.array(trainset.imgs)[:, 1].astype(int)[inds_noisy]],
                 bins=20, range=(0., 1.), edgecolor='black', alpha=0.5, label='noisy')
        plt.xlabel('probability');
        plt.ylabel('number of data')
        plt.grid()
        plt.savefig(opt.save_dir + '/histogram_sep_epoch%03d.jpg' % (epoch))
        plt.clf()
