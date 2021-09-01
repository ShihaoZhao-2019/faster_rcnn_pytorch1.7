import os
import datetime

import torch

import transforms
from network_files import FasterRCNN, FastRCNNPredictor
from backbone import resnet50_fpn_backbone
from my_dataset import VOC2012DataSet
from train_utils import train_eval_utils as utils

if hasattr(torch.cuda, 'empty_cache'):
	torch.cuda.empty_cache()
def create_model(num_classes, device):
    # 注意，这里的backbone默认使用的是FrozenBatchNorm2d，即不会去更新bn参数
    # 目的是为了防止batch_size太小导致效果更差(如果显存很小，建议使用默认的FrozenBatchNorm2d)
    # 如果GPU显存很大可以设置比较大的batch_size就可以将norm_layer设置为普通的BatchNorm2d
    # trainable_layers包括['layer4', 'layer3', 'layer2', 'layer1', 'conv1']， 5代表全部训练
    backbone = resnet50_fpn_backbone(norm_layer=torch.nn.BatchNorm2d,
                                     trainable_layers=3)
    # 训练自己数据集时不要修改这里的91，修改的是传入的num_classes参数
    model = FasterRCNN(backbone=backbone, num_classes=91)
    # 载入预训练模型权重
    # https://download.pytorch.org/models/fasterrcnn_resnet50_fpn_coco-258fb6c6.pth
    weights_dict = torch.load("./backbone/fasterrcnn_resnet50_fpn_coco.pth", map_location=device)
    missing_keys, unexpected_keys = model.load_state_dict(weights_dict, strict=False)
    if len(missing_keys) != 0 or len(unexpected_keys) != 0:
        print("missing_keys: ", missing_keys)
        print("unexpected_keys: ", unexpected_keys)

    # get number of input features for the classifier
    in_features = model.roi_heads.box_predictor.cls_score.in_features
    # replace the pre-trained head with a new one
    model.roi_heads.box_predictor = FastRCNNPredictor(in_features, num_classes)

    return model


def main(parser_data):
   device = torch.device(parser_data.device if torch.cuda.is_available() else "cpu")
   #device = torch.device("cpu")
   print("Using {} device training.".format(device.type))

    # 用来保存coco_info的文件
   results_file = "results{}.txt".format(datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))

   data_transform = {
        "train": transforms.Compose([transforms.ToTensor(),
                                     transforms.RandomHorizontalFlip(0.5)]),
        "val": transforms.Compose([transforms.ToTensor()])
    }

   VOC_root = parser_data.data_path
    # check voc root
   if os.path.exists(os.path.join(VOC_root, "VOCdevkit")) is False:
       raise FileNotFoundError("VOCdevkit dose not in path:'{}'.".format(VOC_root))

   # load train data set
   # VOCdevkit -> VOC2012 -> ImageSets -> Main -> train.txt
   train_data_set = VOC2012DataSet(VOC_root, data_transform["train"], "train.txt")

   # 注意这里的collate_fn是自定义的，因为读取的数据包括image和targets，不能直接使用默认的方法合成batch
   batch_size = parser_data.batch_size
   nw = min([os.cpu_count(), batch_size if batch_size > 1 else 0, 8])  # number of workers
   print('Using %g dataloader workers' % nw)
   train_data_loader = torch.utils.data.DataLoader(train_data_set,
                                                   #batch_size=batch_size,
                                                   batch_size=1,
                                                   shuffle=True,
                                                   pin_memory=True,
                                                   #num_workers=nw,
                                                   num_workers=8,
                                                   collate_fn=train_data_set.collate_fn)

   # load validation data set
   # VOCdevkit -> VOC2012 -> ImageSets -> Main -> val.txt
   val_data_set = VOC2012DataSet(VOC_root, data_transform["val"], "val.txt")
   val_data_set_loader = torch.utils.data.DataLoader(val_data_set,
                                                     #batch_size=batch_size,
                                                     batch_size=1,
                                                     shuffle=False,
                                                     pin_memory=True,
                                                     #num_workers=nw,
                                                     num_workers=8,
                                                     collate_fn=train_data_set.collate_fn)

   # create model num_classes equal background + 20 classes
   model = create_model(num_classes=parser_data.num_classes + 1, device=device)
   # print(model)

   model.to(device)

   # define optimizer
   params = [p for p in model.parameters() if p.requires_grad]
   optimizer = torch.optim.SGD(params, lr=0.005,
                               momentum=0.9, weight_decay=0.0005)

   # learning rate scheduler
   lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer,
                                                  step_size=3,
                                                  gamma=0.33)

   # 如果指定了上次训练保存的权重文件地址，则接着上次结果接着训练
   if parser_data.resume != "":
       checkpoint = torch.load(parser_data.resume, map_location=device)
       model.load_state_dict(checkpoint['model'])
       optimizer.load_state_dict(checkpoint['optimizer'])
       lr_scheduler.load_state_dict(checkpoint['lr_scheduler'])
       parser_data.start_epoch = checkpoint['epoch'] + 1
       print("the training process from epoch{}...".format(parser_data.start_epoch))

   train_loss = []
   learning_rate = []
   val_map = []

   for epoch in range(parser_data.start_epoch, parser_data.epochs):
       # train for one epoch, printing every 10 iterations
       mean_loss, lr = utils.train_one_epoch(model, optimizer, train_data_loader,
                                             device=device, epoch=epoch,
                                             print_freq=50, warmup=True)
       train_loss.append(mean_loss.item())
       learning_rate.append(lr)

       # update the learning rate
       lr_scheduler.step()

       # evaluate on the test dataset
       coco_info = utils.evaluate(model, val_data_set_loader, device=device)

       # write into txt
       with open(results_file, "a") as f:
           # 写入的数据包括coco指标还有loss和learning rate
           result_info = [str(round(i, 4)) for i in coco_info + [mean_loss.item()]] + [str(round(lr, 6))]
           txt = "epoch:{} {}".format(epoch, '  '.join(result_info))
           f.write(txt + "\n")

       val_map.append(coco_info[1])  # pascal mAP

       # save weights
       save_files = {
           'model': model.state_dict(),
           'optimizer': optimizer.state_dict(),
           'lr_scheduler': lr_scheduler.state_dict(),
           'epoch': epoch}
       torch.save(save_files, "./save_weights/resNetFpn-model-{}.pth".format(epoch))

   # plot loss and lr curve
   if len(train_loss) != 0 and len(learning_rate) != 0:
       from plot_curve import plot_loss_and_lr
       plot_loss_and_lr(train_loss, learning_rate)

   # plot mAP curve
   if len(val_map) != 0:
       from plot_curve import plot_map
       plot_map(val_map)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description=__doc__)

    # 训练设备类型
    parser.add_argument('--device', default='cuda:0', help='device')
    # 训练数据集的根目录(VOCdevkit)
    parser.add_argument('--data-path', default='./', help='dataset')
    # 检测目标类别数(不包含背景)
    parser.add_argument('--num-classes', default=20, type=int, help='num_classes')
    # 文件保存地址
    parser.add_argument('--output-dir', default='./save_weights', help='path where to save')
    # 若需要接着上次训练，则指定上次训练保存权重文件地址
    parser.add_argument('--resume', default='', type=str, help='resume from checkpoint')
    # 指定接着从哪个epoch数开始训练
    parser.add_argument('--start_epoch', default=0, type=int, help='start epoch')
    # 训练的总epoch数
    parser.add_argument('--epochs', default=15, type=int, metavar='N',
                        help='number of total epochs to run')
    # 训练的batch size
    parser.add_argument('--batch_size', default=8, type=int, metavar='N',
                        help='batch size when training.')

    args = parser.parse_args()
    print(args)

    # 检查保存权重文件夹是否存在，不存在则创建
    if not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)

    main(args)
