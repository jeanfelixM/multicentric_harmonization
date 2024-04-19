import json
import os
import queue
import re
import numpy as np
from sklearn.model_selection import GroupShuffleSplit
from tqdm import tqdm
import torch
from torch.optim.lr_scheduler import CosineAnnealingLR
import torch.optim as optim
from monai.data import DataLoader, Dataset
from monai.transforms import Compose, LoadImaged, EnsureChannelFirstd, AsDiscreted, ToTensord
from swinunetr import CropOnROId, custom_collate_fn,DebugTransform
from monai.networks.nets import SwinUNETR
from pytorch_metric_learning.losses import NTXentLoss

class Train:
    
    def __init__(self, model, data_loader, optimizer, lr_scheduler, num_epoch, classifier=None, acc_metric='total_mean', contrast_loss=NTXentLoss(temperature=0.20)):
        self.model = model
        self.classifier = classifier
        self.data_loader = data_loader
        self.optimizer = optimizer
        self.lr_scheduler = lr_scheduler
        self.num_epoch = num_epoch
        self.contrast_loss = contrast_loss
        self.classification_loss = torch.nn.CrossEntropyLoss()
        self.acc_metric = acc_metric
        self.batch_size = data_loader['train'].batch_size

        self.epoch = 0
        self.log_summary_interval = 2
        self.total_progress_bar = tqdm(total=self.num_epoch, desc='Total Progress', dynamic_ncols=True)
        self.acc_dict = {'src_best_train_acc': 0, 'src_best_test_acc': 0, 'tgt_best_test_acc': 0}
        self.losses_dict = {'total_loss': 0, 'src_classification_loss': 0, 'contrast_loss': 0}
        self.log_dict = {'src_train_acc': 0, 'src_test_acc': 0, 'tgt_test_acc': 0}
        self.best_acc_dict = {'src_best_train_acc': 0, 'src_best_test_acc': 0, 'tgt_best_test_acc': 0}
        self.best_loss_dict = {'total_loss': float('inf'), 'src_classification_loss': float('inf'), 'contrast_loss': float('inf')}
        self.best_log_dict = {'src_train_acc': 0, 'src_test_acc': 0, 'tgt_test_acc': 0}
        
    
    def train(self):
            self.total_progress_bar.write('Start training')

            while self.epoch < self.num_epoch:
                self.train_loader = self.data_loader['train'] #il faudra que le dataloader monai ne mette pas dans le meme batch des ct scan de la meme serie (cad des memes repetitions d'un scan) -> voir Sampler pytorch
                self.test_loader = self.data_loader['test']
                self.train_epoch()
                if self.epoch % self.log_summary_interval == 0:
                    self.test_epoch()
                    self.log_summary_writer()
                self.lr_scheduler.step()

            self.total_progress_bar.write('Finish training')
            self.save_model('./model_final_weights.pth')
            return self.acc_dict['best_test_acc']

    def train_epoch(self):
        epoch_iterator = tqdm(self.train_loader, desc="Training (X / X Steps) (loss=X.X)", dynamic_ncols=True)
        total_batches = len(self.train_loader)
        running_loss = 0
        
        for step, batch in enumerate(epoch_iterator):
            self.model.train()
            loss,classif_acc = self.train_step(batch)
            running_loss += loss.item()
            average_loss = running_loss / (step + 1)
            epoch_iterator.set_description("Training ({}/ {}) (loss={:.4f}), classif_acc={:.4f}".format(step + 1, total_batches, average_loss,classif_acc))
            epoch_iterator.refresh()
        self.total_progress_bar.update(1)
        self.epoch += 1
        
    def train_step(self,batch):
        # update the learning rate of the optimizer
        self.optimizer.zero_grad()

        # prepare batch
        imgs_s = batch["image"].cuda()
        all_labels = batch["roi"].cuda()
        ids = batch["id"] 

        # model inference
        latents = self.model.swinViT(imgs_s)
        bottleneck = latents[4]
        
        self.contrastive_step(latents,ids)
        
        features = torch.mean(bottleneck, dim=(2, 3, 4))
        accu = self.classification_step(features, all_labels)
        print(f"Train Accuracy: {accu}%")
        
        #image reconstruction
        #reconstructed_imgs = self.reconstruct_image(latents)

        if self.epoch >= 0:
            self.losses_dict['total_loss'] = \
            self.losses_dict['classification_loss'] + self.losses_dict['contrast_loss']
        else:
            self.losses_dict['total_loss'] = self.losses_dict['classification_loss']

        self.losses_dict['total_loss'].backward()
        self.optimizer.step()

        
        return self.losses_dict['total_loss'], accu

    def classification_step(self, features, labels):
        if self.classifier is None:
            self.classfier = self.autoclassifier(features.size(1), labels.max().item() + 1)
        logits = self.classifier(features)
        classification_loss = self.classification_loss(logits, labels)
        self.losses_dict['classification_loss'] = classification_loss

        return compute_accuracy(logits, labels, acc_metric=self.acc_metric)

    def contrastive_step(self, latents,ids): #actuellement la loss contrastive est aussi calculé entre sous patchs de la même image, on voudrait eviter ça idealement
        contrast_loss = 0
        for id in torch.unique(ids):
            boolids = (ids == id)
        
            #bottleneck
            btneck = latents[4]  # (batch_size, 768, D, H, W)
            btneck = btneck[boolids]
            num_elements = btneck.shape[2] * btneck.shape[3] * btneck.shape[4]
        
            # (batch_size, D, H, W, 768) -> (batch_size * num_elements, 768)
            embeddings = btneck.permute(0, 2, 3, 4, 1).reshape(-1, 768)
            labels = torch.arange(num_elements).repeat(btneck.shape[0]) 
            weigth = btneck.shape[0] / self.batch_size
            contrast_loss += weigth * self.loss_func(embeddings, labels)['losses']
            
        self.losses_dict['contrast_loss'] = contrast_loss
        
    def test_epoch(self):
        self.model.eval()
        total_test_accuracy = []
        with torch.no_grad():
            testing_iterator = tqdm(self.test_loader, desc="Testing (X / X Steps) (loss=X.X)", dynamic_ncols=True)
            for step,batch in enumerate(testing_iterator):
                imgs_s = batch["image"].cuda()
                all_labels = batch["roi"].cuda()
                logits = self.classifier(torch.mean(self.model.swinViT(imgs_s)[4], dim=(2, 3, 4)))
                test_accuracy = compute_accuracy(logits, all_labels, acc_metric=self.acc_metric)
                total_test_accuracy.append(test_accuracy)
                testing_iterator.set_description(f"Testing ({step + 1}/{len(self.test_loader)}) (accuracy={test_accuracy:.4f})")
        avg_test_accuracy = np.mean(total_test_accuracy)
        self.acc_dict['best_test_acc'] = avg_test_accuracy
        print(f"Test Accuracy: {avg_test_accuracy}%") 
        
    def autoclassifier(self, in_features, num_classes):
        #simple mlp with dropout
        classifier = torch.nn.Sequential(
            torch.nn.Linear(in_features, 512),
            torch.nn.ReLU(),
            torch.nn.Dropout(0.2),
            torch.nn.Linear(512, num_classes)
        ).cuda()
        return classifier 
    
    def save_model(self, path):
        torch.save(self.model.state_dict(), path)
        print(f'Model weights saved to {path}')

def compute_accuracy(logits, true_labels, acc_metric='total_mean', print_result=False):
    assert logits.size(0) == true_labels.size(0)
    if acc_metric == 'total_mean':
        predictions = torch.max(logits, dim=1)[1]
        accuracy = 100.0 * (predictions == true_labels).sum().item() / logits.size(0)
        if print_result:
            print(accuracy)
        return accuracy
    elif acc_metric == 'class_mean':
        num_classes = logits.size(1)
        predictions = torch.max(logits, dim=1)[1]
        class_accuracies = []
        for class_label in range(num_classes):
            class_mask = (true_labels == class_label)

            class_count = class_mask.sum().item()
            if class_count == 0:
                class_accuracies += [0.0]
                continue

            class_accuracy = 100.0 * (predictions[class_mask] == class_label).sum().item() / class_count
            class_accuracies += [class_accuracy]
        if print_result:
            print(f'class_accuracies: {class_accuracies}')
            print(f'class_mean_accuracies: {torch.mean(class_accuracies)}')
        return torch.mean(class_accuracies)
    else:
        raise ValueError(f'acc_metric, {acc_metric} is not available.')
    
def load_json(json_path):
    with open(json_path, 'r') as file:
        data_list = json.load(file)
    return data_list

def extract_base(description):
    """Extract the base part of the description, excluding any numeric suffix."""
    match = re.match(r"(.+)(-\s#\d+)$", description)
    if match:
        return match.group(1).strip()
    return description

def group_data(data_list, mode='scanner'):
    group_map = {}
    for item in data_list:
        if mode == 'scanner':
            group_key = item['info']['SeriesDescription'][:2]
        elif mode == 'repetition':
            group_key = extract_base(item['info']['SeriesDescription'])
        
        if group_key not in group_map:
            group_map[group_key] = len(group_map)
        item['group_id'] = group_map[group_key]
    return data_list

def create_datasets(data_list, test_size=0.2, seed=42):
    data_list = group_data(data_list, mode='scanner') 
    groups = [item['group_id'] for item in data_list]
    
    splitter = GroupShuffleSplit(n_splits=1, test_size=test_size, random_state=seed)
    train_idx, test_idx = next(splitter.split(data_list, groups=groups))

    train_data = [data_list[i] for i in train_idx]
    test_data = [data_list[i] for i in test_idx]

    return train_data, test_data


def get_model():
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu") 
    target_size = (64, 64, 32)  
    model = SwinUNETR(
        img_size=target_size,
        in_channels=1,
        out_channels=14,
        feature_size=48,
        use_checkpoint=True,
    ).to(device)

    weight = torch.load("./model_swinvit.pt")
    model.load_from(weights=weight)
    model = model.to('cuda')
    return model

def main():
    transforms = Compose([
        LoadImaged(keys=["image", "roi"]),
        EnsureChannelFirstd(keys=["image", "roi"]),
        CropOnROId(keys=["image"], roi_key="roi", size=(32, 32, 32)), 
        AsDiscreted(keys=["roi_label"], n_classes=6),
        DebugTransform(),
        ToTensord(keys=["image", "roi_label"])
    ])

    jsonpath = "./dataset_info.json"
    data_list = load_json(jsonpath)
    train_data, test_data = create_datasets(data_list)

    train_loader = DataLoader(Dataset(data=train_data, transform=transforms), batch_size=1, shuffle=True,collate_fn=custom_collate_fn)
    test_loader = DataLoader(Dataset(data=test_data, transform=transforms), batch_size=1, shuffle=False,collate_fn=custom_collate_fn)
    data_loader = {'train': train_loader, 'test': test_loader}
    
    model = get_model()
    optimizer = optim.AdamW(model.parameters(), lr=1e-4, weight_decay=0.01)
    lr_scheduler = CosineAnnealingLR(optimizer, T_max=50, eta_min=1e-6)
    
    trainer = Train(model, data_loader, optimizer, lr_scheduler, 10)
    
    trainer.train()

if __name__ == '__main__':
    main()
