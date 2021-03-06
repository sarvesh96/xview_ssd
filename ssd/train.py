import os
import sys
import time
import torch
import visdom
import argparse
import numpy as np

import torch.nn as nn
import torch.optim as optim
import torch.nn.init as init
import torch.backends.cudnn as cudnn

from data import *
from ssd import build_ssd
from torch.autograd import Variable
from tensorboardX import SummaryWriter
from layers.modules import MultiBoxLoss
from utils.augmentations import SSDAugmentation
from torch.utils.data.dataloader import DataLoader


parser = argparse.ArgumentParser(
	description='Single Shot MultiBox Detector Training for XVIEW Dataset With \
				 Pytorch')
train_set = parser.add_mutually_exclusive_group()
parser.add_argument('--dataset', default='XVIEW', choices=['XVIEW'],
					type=str, help='Name of Dataset. Choices - [XVIEW]')
parser.add_argument('--dataset_root', default=XVIEW_ROOT,
					help='Dataset root directory path')
parser.add_argument('--basenet', default='vgg16_reducedfc.pth',
					help='Pretrained base model filename.')
parser.add_argument('--batch_size', default=32, type=int,
					help='Batch size for training')
parser.add_argument('--resume', default=None, type=str,
					help='Checkpoint state_dict file to resume training from')
parser.add_argument('--start_iter', default=0, type=int,
					help='Resume training at this iter')
parser.add_argument('--num_workers', default=4, type=int,
					help='Number of workers used in dataloading')
parser.add_argument('--cuda', default=True, action='store_false',
					help='Do not use CUDA to train model')
parser.add_argument('--lr', '--learning-rate', default=1e-3, type=float,
					help='initial learning rate')
parser.add_argument('--momentum', default=0.9, type=float,
					help='Momentum value for optim')
parser.add_argument('--weight_decay', default=5e-4, type=float,
					help='Weight decay for SGD')
parser.add_argument('--gamma', default=0.1, type=float,
					help='Gamma update for SGD')
parser.add_argument('--visdom', action='store_true',
					help='Use visdom for loss visualization')
parser.add_argument('--save_folder', default='weights/',
					help='Directory for saving checkpoint models')
parser.add_argument('--num_epochs', default=10, type=int,
					help='Number of epochs on the data')
parser.add_argument('--logdir', default='outputs/logs/', type=str,
					help='tensorboard log directory.')
parser.add_argument('--start_global_step', default=0, type=int,
					help='Resume global_step value at this step.')
parser.add_argument('--validate', default=False, action='store_true',
					help='If set to True, validation scores are calculated.')
parser.add_argument('--images_filename', type=str,
					default='../../Data/chipped/images_300_train.npy',
					help='location of _images.npy')
parser.add_argument('--boxes_filename', type=str,
					default='../../Data/chipped/boxes_300_train.npy',
					help='location of _boxes.npy')
parser.add_argument('--classes_filename', type=str,
					default='../../Data/chipped/classes_300_train.npy',
					help='location of _classes.npy')
parser.add_argument('--val_images_filename', type=str,
                    default='../../Data/chipped/images_300_val.npy',
					help='location of _images.npy')
parser.add_argument('--val_boxes_filename', type=str,
                    default='../../Data/chipped/boxes_300_val.npy',
					help='location of _boxes.npy')
parser.add_argument('--val_classes_filename', type=str,
                    default='../../Data/chipped/classes_300_val.npy',
					help='location of _classes.npy')
parser.add_argument('--print_to_file', default=False, action='store_true',
					help='If set to True, outputs are printed to file. \
						  Requires the print_filename argument to be given.')
parser.add_argument('--print_filename', default="outputs/log.txt", type=str,
					help='File path and name to store the outputs.')
args = parser.parse_args()


torch.set_default_tensor_type('torch.FloatTensor')
if torch.cuda.is_available():
	if args.cuda:
		torch.set_default_tensor_type('torch.cuda.FloatTensor')
	if not args.cuda:
		print("WARNING: It looks like you have a CUDA device, but aren't " +
			  "using CUDA.\nRun with --cuda for optimal training speed.")

if not os.path.exists(args.save_folder):
	os.mkdir(args.save_folder)

if not os.path.exists(args.logdir):
	os.mkdir(args.logdir)

if args.visdom:
	viz = visdom.Visdom()

if args.print_to_file:
	f = open(args.print_filename, 'w')

def train():
	if args.dataset == 'XVIEW':
		if args.dataset_root != XVIEW_ROOT:
			if not os.path.exists(XVIEW_ROOT):
				parser.error('Must specify dataset_root if specifying dataset')
			print("WARNING: Using default COCO dataset_root because " +
				  "--dataset_root was not specified.")
			args.dataset_root = XVIEW_ROOT
		cfg = xview
		train_dataset = XVIEWDetection(args.images_filename,
		                         args.boxes_filename,
								 args.classes_filename,
								 transform=SSDAugmentation(cfg['min_dim'],
														   MEANS))
		val_dataset = XVIEWDetection(args.val_images_filename,
		                         args.val_boxes_filename,
								 args.val_classes_filename,
								 transform=SSDAugmentation(cfg['min_dim'],
														   MEANS))

	writer = SummaryWriter(args.logdir)

	ssd_net = build_ssd('train', cfg['min_dim'], cfg['num_classes'])
	net = ssd_net

	if args.cuda:
		net = torch.nn.DataParallel(ssd_net)
		cudnn.benchmark = True

	if args.resume:
		print('Resuming training, loading {}...'.format(args.resume))
		ssd_net.load_weights(args.resume)
	else:
		vgg_weights = torch.load(args.save_folder + args.basenet)
		print('Loading base network...')
		ssd_net.vgg.load_state_dict(vgg_weights)

	if args.cuda:
		net = net.cuda()

	if not args.resume:
		# initialize newly added layers' weights with xavier method
		print('Initializing weights...')
		ssd_net.extras.apply(weights_init)
		ssd_net.loc.apply(weights_init)
		ssd_net.conf.apply(weights_init)

	optimizer = optim.SGD(net.parameters(), lr=args.lr, momentum=args.momentum,
						  weight_decay=args.weight_decay)
	criterion = MultiBoxLoss(cfg['num_classes'], 0.5, True, 0, True, 3, 0.5,
							 False, args.cuda)

	net.train()
	# loss counters
	loc_loss = 0
	conf_loss = 0
	epoch = 0
	print('Loading the dataset...')

	epoch_size = len(train_dataset) // args.batch_size
	print('Training SSD on:', train_dataset.name)
	print('Using the specified args:')
	print(args)

	step_index = 0
	global_step = 0

	if args.visdom:
		vis_title = 'SSD.PyTorch on ' + train_dataset.name
		vis_legend = ['Loc Loss', 'Conf Loss', 'Total Loss']
		val_plot = create_vis_plot('Validation', 'Loss', vis_title, vis_legend)
		iter_plot = create_vis_plot('Iteration', 'Loss', vis_title, vis_legend)
		epoch_plot = create_vis_plot('Epoch', 'Loss', vis_title, vis_legend)


	train_data_loader = DataLoader(train_dataset, args.batch_size,
								  num_workers=args.num_workers,
								  shuffle=True, collate_fn=detection_collate,
								  pin_memory=True)

	val_data_loader = DataLoader(val_dataset, args.batch_size,
								  num_workers=args.num_workers,
								  shuffle=True, collate_fn=detection_collate,
								  pin_memory=True)

	eph_num = 0

	# # create batch iterator
	# batch_iterator = iter(train_data_loader)
	# for iteration in range(args.start_iter, cfg['max_iter']):
	# while eph_num < args.num_epochs:
	while True:
		eph_num += 1

		for iteration, (images, targets) in enumerate(train_data_loader):
			global_step += 1

			if args.visdom and iteration != 0 and (iteration % epoch_size == 0):
				update_vis_plot(epoch, loc_loss, conf_loss, epoch_plot, None,
								'append', epoch_size)
				# reset epoch loss counters
				loc_loss = 0
				conf_loss = 0
				epoch += 1

			if iteration in cfg['lr_steps']:
				step_index += 1
				adjust_learning_rate(optimizer, args.gamma, step_index)

			# # load train data
			# images, targets = next(batch_iterator)

			if args.cuda:
				images = Variable(images.cuda())
				targets = [Variable(ann.cuda(), requires_grad=True) for ann in targets]
			else:
				images = Variable(images)
				targets = [Variable(ann, requires_grad=True) for ann in targets]

			# forward
			t0 = time.time()
			out = net(images)

			# backprop
			optimizer.zero_grad()
			loss_l, loss_c = criterion(out, targets)

			loss = loss_l + loss_c

			writer.add_scalar('Train-Loc Loss:', loss_l, global_step)
			writer.add_scalar('Train-Conf Loss:', loss_c, global_step)
			writer.add_scalar('Train-Total Loss:', loss, global_step)

			loss.backward()
			optimizer.step()

			t1 = time.time()

			# print('Loc Loss:{:>10.4f}| Conf Loss:{:10.4f}'.format(loss_l, loss_c))
			loc_loss += loss_l.data
			conf_loss += loss_c.data

			if iteration % 10 == 0:
				print('timer: %.4f sec.' % (t1 - t0))
				print('iter ' + repr(iteration) + ' || Loss: %.4f ||' % (loss.data))#, end=' ')
				if args.print_to_file:
					f.write('timer: %.4f sec.\n' % (t1 - t0))
					f.write('iter ' + repr(iteration) + ' || Loss: %.4f ||\n' % (loss.data))

			if args.visdom:
				init_epoch = True if eph_num == 0 else False
				update_vis_plot(iteration, loss_l.data, loss_c.data,
								iter_plot, epoch_plot, 'append',
								init_epoch=init_epoch)

			# if iteration != 0 and iteration % 5000 == 0:
			# 	print('Saving state, iter:', iteration)
			# 	torch.save(ssd_net.state_dict(), 'weights/ssd300_XVIEW_' +
			# 			   repr(iteration) + '.pth')

		if eph_num % 3 == 0:
			print('Saving state, epoch:', eph_num)
			state_dict_filename = args.dataset + '_' + str(eph_num) + '_new.pth'
			torch.save(ssd_net.state_dict(),
					   args.save_folder + state_dict_filename)

		# Validate Model
		if args.validate and eph_num % 2 == 0:
			print('Validating Model...')
			running_loc_loss, running_conf_loss, iters = 0.0, 0.0, 0
			t0 = time.time()

			for iteration, (images, targets) in enumerate(val_data_loader):
				if args.cuda:
					images = Variable(images.cuda())
					targets = [Variable(ann.cuda(), requires_grad=True) for ann in targets]
				else:
					images = Variable(images)
					targets = [Variable(ann, requires_grad=True) for ann in targets]

				# forward
				out = net(images)

				loss_l, loss_c = criterion(out, targets)
				running_loc_loss += loss_l.data
				running_conf_loss += loss_c.data
				iters += 1

			t1 = time.time()
			running_loc_loss /= iters
			running_conf_loss /= iters
			total_loss = running_loc_loss + running_conf_loss

			print('timer: %.4f sec.' % (t1 - t0))
			print('epoch ' + str(eph_num) + ' || Loss: %.4f ||' % (total_loss))
			if args.print_to_file:
				f.write('timer: %.4f sec.\n' % (t1 - t0))
				f.write('epoch ' + str(eph_num) + ' || Loss: %.4f ||\n' % (total_loss))

			if args.visdom:
				init_epoch = True if eph_num == 2 else False
				update_vis_plot(eph_num//2, running_loc_loss, running_conf_loss,
								val_plot, None, 'append', init_epoch=init_epoch)

			writer.add_scalar('Val-Loc Loss:', running_loc_loss, global_step)
			writer.add_scalar('Val-Conf Loss:', running_conf_loss, global_step)
			writer.add_scalar('Val-Total Loss:', total_loss, global_step)

	writer.close()


def adjust_learning_rate(optimizer, gamma, step):
	"""Sets the learning rate to the initial LR decayed by 10 at every
		specified step
	# Adapted from PyTorch Imagenet example:
	# https://github.com/pytorch/examples/blob/master/imagenet/main.py
	"""
	lr = args.lr * (gamma ** (step))
	for param_group in optimizer.param_groups:
		param_group['lr'] = lr


def xavier(param):
	init.xavier_uniform(param)


def weights_init(m):
	if isinstance(m, nn.Conv2d):
		xavier(m.weight.data)
		m.bias.data.zero_()


def create_vis_plot(_xlabel, _ylabel, _title, _legend):
	return viz.line(
		X=torch.zeros((1,)).cpu(),
		Y=torch.zeros((1, 3)).cpu(),
		opts=dict(
			xlabel=_xlabel,
			ylabel=_ylabel,
			title=_title,
			legend=_legend
		)
	)


def update_vis_plot(iteration, loc, conf, window1, window2, update_type,
					epoch_size=1, init_epoch=False):
	viz.line(
		X=torch.ones((1, 3)).cpu() * iteration,
		Y=torch.Tensor([loc, conf, loc + conf]).unsqueeze(0).cpu() / epoch_size,
		win=window1,
		update=update_type
	)
	# initialize epoch plot on first iteration
	if init_epoch and iteration == 0:
		viz.line(
			X=torch.zeros((1, 3)).cpu(),
			Y=torch.Tensor([loc, conf, loc + conf]).unsqueeze(0).cpu(),
			win=window2,
			update=True
		)


if __name__ == '__main__':
	train()
