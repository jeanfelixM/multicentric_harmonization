import json
import os
import queue
import re
from matplotlib import pyplot as plt
import numpy as np
from sklearn.model_selection import GroupShuffleSplit
from tqdm import tqdm
from analyze.analyze import perform_tsne
import torch
from torch.optim.lr_scheduler import CosineAnnealingLR
import torch.optim as optim
from pytorch_msssim import ssim
from monai.data import DataLoader, Dataset,CacheDataset,PersistentDataset,SmartCacheDataset,ThreadDataLoader
from monai.transforms import Compose, LoadImaged, EnsureChannelFirstd, AsDiscreted, ToTensord,EnsureTyped
from harmonization.swin_contrastive.swinunetr import CropOnROId, custom_collate_fn,DebugTransform, get_model, load_data
from monai.networks.nets import SwinUNETR
from pytorch_metric_learning.losses import NTXentLoss
from monai.transforms import Transform
import torch.nn as nn
import imageio
import nibabel as nib

class ReconstructionLoss(nn.Module):
    def __init__(self, ssim_weight=0.5):
        super(ReconstructionLoss, self).__init__()
        self.l1_loss = nn.L1Loss()
        self.ssim_weight = ssim_weight

    def forward(self, output, target):
        l1 = self.l1_loss(output, target)
        ssim_loss = 1 - ssim(output, target, data_range=1.0, size_average=True)
        total_loss = l1 + self.ssim_weight * ssim_loss
        return total_loss

class Train:
    
    def __init__(self, model, data_loader, optimizer, lr_scheduler, num_epoch, dataset, classifier=None, acc_metric='total_mean', contrast_loss=NTXentLoss(temperature=0.20), contrastive_latentsize=768,savename='model.pth'):
        self.model = model
        self.in_channels = 1
        self.classifier = classifier
        self.data_loader = data_loader
        self.optimizer = optimizer
        self.lr_scheduler = lr_scheduler
        self.num_epoch = num_epoch
        self.contrast_loss = contrast_loss
        self.classification_loss = torch.nn.CrossEntropyLoss().cuda()
        self.device = self.get_device()
        self.recons_loss = ReconstructionLoss(ssim_weight=1).to(self.device)
        self.acc_metric = acc_metric
        self.batch_size = data_loader['train'].batch_size
        self.dataset = dataset['train']
        self.testdataset = dataset['test']
        self.contrastive_latentsize = contrastive_latentsize
        self.save_name = savename
        self.reconstruct = self.get_reconstruction_model()
        
        #quick fix to load reconstruction model
        self.load_reconstruction_model('FT_whole_RECONSTRUCTION_model_reconstruction.pth')
        
        #quick fix to train decoder only
        self.optimizer = optim.Adam(self.reconstruct.parameters(), lr=1e-3)
        
        self.epoch = 0
        self.log_summary_interval = 5
        self.step_interval = 10
        self.total_progress_bar = tqdm(total=self.num_epoch, desc='Total Progress', dynamic_ncols=True)
        self.acc_dict = {'src_best_train_acc': 0, 'src_best_test_acc': 0, 'tgt_best_test_acc': 0}
        self.losses_dict = {'total_loss': 0, 'src_classification_loss': 0, 'contrast_loss': 0}
        self.log_dict = {'src_train_acc': 0, 'src_test_acc': 0, 'tgt_test_acc': 0}
        self.best_acc_dict = {'src_best_train_acc': 0, 'src_best_test_acc': 0, 'tgt_best_test_acc': 0}
        self.best_loss_dict = {'total_loss': float('inf'), 'src_classification_loss': float('inf'), 'contrast_loss': float('inf')}
        self.best_log_dict = {'src_train_acc': 0, 'src_test_acc': 0, 'tgt_test_acc': 0}
        self.tsne_plots = []
        
        self.train_losses = {'contrast_losses': [], 'classification_losses': [], 'reconstruction_losses': [], 'total_losses': []}
    
    def get_device(self):
        device_id = 0
        os.environ["CUDA_VISIBLE_DEVICES"] = str(device_id)
        torch.cuda.set_device(device_id)
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        return device
        
    def get_reconstruction_model(self, reconstruction_type='vae',dim=768):
        if reconstruction_type == 'vae':
            model = nn.Sequential(
                nn.Conv3d(dim, dim // 2, kernel_size=3, stride=1, padding=1),
                nn.InstanceNorm3d(dim // 2),
                nn.LeakyReLU(),
                nn.Upsample(scale_factor=2, mode="trilinear", align_corners=False),
                nn.Conv3d(dim // 2, dim // 4, kernel_size=3, stride=1, padding=1),
                nn.InstanceNorm3d(dim // 4),
                nn.LeakyReLU(),
                nn.Upsample(scale_factor=2, mode="trilinear", align_corners=False),
                nn.Conv3d(dim // 4, dim // 8, kernel_size=3, stride=1, padding=1),
                nn.InstanceNorm3d(dim // 8),
                nn.LeakyReLU(),
                nn.Upsample(scale_factor=2, mode="trilinear", align_corners=False),
                nn.Conv3d(dim // 8, dim // 16, kernel_size=3, stride=1, padding=1),
                nn.InstanceNorm3d(dim // 16),
                nn.LeakyReLU(),
                nn.Upsample(scale_factor=2, mode="trilinear", align_corners=False),
                nn.Conv3d(dim // 16, dim // 16, kernel_size=3, stride=1, padding=1),
                nn.InstanceNorm3d(dim // 16),
                nn.LeakyReLU(),
                nn.Upsample(scale_factor=2, mode="trilinear", align_corners=False),
                nn.Conv3d(dim // 16, self.in_channels, kernel_size=1, stride=1),
            )
            model.to(self.device)
            return model
        elif reconstruction_type == 'deconv':
            model= nn.Sequential(
                nn.ConvTranspose3d(dim, dim // 2, kernel_size=(2, 2, 2), stride=(2, 2, 2)),
                nn.ConvTranspose3d(dim // 2, dim // 4, kernel_size=(2, 2, 2), stride=(2, 2, 2)),
                nn.ConvTranspose3d(dim // 4, dim // 8, kernel_size=(2, 2, 2), stride=(2, 2, 2)),
                nn.ConvTranspose3d(dim // 8, dim // 16, kernel_size=(2, 2, 2), stride=(2, 2, 2)),
                nn.ConvTranspose3d(dim // 16, self.in_channels, kernel_size=(2, 2, 2), stride=(2, 2, 2)),
            )
            model.to(self.device)
            return model
        else:
            raise ValueError(f"Invalid reconstruction type: {reconstruction_type}")
        
    def load_reconstruction_model(self, path):
        weights = torch.load(path)
        self.reconstruct.load_state_dict(weights)
        self.reconstruct.eval()
        print(f'Model weights loaded from {path}')
        
    def save_losses(self, train_loss, loss_file='losses.json'):
        self.train_losses['total_losses'].append(train_loss)
        self.train_losses['contrast_losses'].append(self.losses_dict['contrast_loss'])
        self.train_losses['classification_losses'].append(self.losses_dict['classification_loss'])
        self.train_losses['reconstruction_losses'].append(self.losses_dict['reconstruction_loss'])
        #self.val_losses.append(val_loss)
        
        
        # Convert torch.Tensor to a JSON-serializable format
        def convert_to_serializable(obj):
            if isinstance(obj, torch.Tensor):
                return obj.tolist()  # Convert tensor to list
            elif isinstance(obj, list):
                return [convert_to_serializable(item) for item in obj]
            else:
                return obj
        
        #serializable_val_losses = convert_to_serializable(self.val_losses)
        serializable_contrast_losses = convert_to_serializable(self.train_losses['contrast_losses'])
        serializable_classification_losses = convert_to_serializable(self.train_losses['classification_losses'])
        serializable_total_losses = convert_to_serializable(self.train_losses['total_losses'])
        serializable_recosntruction_losses = convert_to_serializable(self.train_losses['reconstruction_losses'])
        
        # with open(loss_file, 'w') as f:
        #     json.dump({'train_losses': serializable_train_losses, 'val_losses': serializable_val_losses}, f)
        with open('contrast_losses.json', 'w') as f:
            json.dump({'contrast_losses': serializable_contrast_losses}, f)
        with open('classification_losses.json', 'w') as f:
            json.dump({'classification_losses': serializable_classification_losses}, f)
        with open('total_losses.json', 'w') as f:
            json.dump({'total_losses': serializable_total_losses}, f)
        with open('reconstruction_losses.json', 'w') as f:
            json.dump({'reconstruction_losses': serializable_recosntruction_losses}, f)
            
    def plot_losses(self):
        step_interval = self.step_interval

        points = len(self.train_losses['contrast_losses'])
        steps = np.arange(0, points * step_interval, step_interval)
        contrast_losses = [loss.detach().numpy() for loss in self.train_losses['contrast_losses']]
        
        fig, ax = plt.subplots(2, 2, figsize=(15, 10))

        ax[0, 0].plot(steps, contrast_losses, label='Contrastive Loss')
        ax[0, 0].set_title('Contrastive Loss')
        ax[0, 0].set_xlabel('Steps')
        ax[0, 0].set_ylabel('Loss')
        ax[0, 0].legend()

        points = len(self.train_losses['classification_losses'])
        steps = np.arange(0, points * step_interval, step_interval)
        classification_losses = [loss.detach().numpy() for loss in self.train_losses['classification_losses']]

        ax[0, 1].plot(steps, classification_losses, label='Classification Loss')
        ax[0, 1].set_title('Classification Loss')
        ax[0, 1].set_xlabel('Steps')
        ax[0, 1].set_ylabel('Loss')
        ax[0, 1].legend()

        points = len(self.train_losses['reconstruction_losses'])
        steps = np.arange(0, points * step_interval, step_interval)
        reconstruction_losses = [loss.detach().numpy() for loss in self.train_losses['reconstruction_losses']]

        ax[1, 0].plot(steps, reconstruction_losses, label='Reconstruction Loss')
        ax[1, 0].set_title('Reconstruction Loss')
        ax[1, 0].set_xlabel('Steps')
        ax[1, 0].set_ylabel('Loss')
        ax[1, 0].legend()

        points = len(self.train_losses['total_losses'])
        steps = np.arange(0, points * step_interval, step_interval)
        total_losses = [loss.detach().numpy() for loss in self.train_losses['total_losses']]

        ax[1, 1].plot(steps, total_losses, label='Total Loss')
        ax[1, 1].set_title('Total Loss')
        ax[1, 1].set_xlabel('Steps')
        ax[1, 1].set_ylabel('Loss')
        ax[1, 1].legend()

        plt.tight_layout()
        plt.savefig('losses_plot.png')
        plt.show()

    
    def train(self):
        self.total_progress_bar.write('Start training')
        self.dataset.start()
        self.testdataset.start()
        self.model.train()
        while self.epoch < self.num_epoch:
            self.train_loader = self.data_loader['train'] #il faudra que le dataloader monai ne mette pas dans le meme batch des ct scan de la meme serie (cad des memes repetitions d'un scan) -> voir Sampler pytorch
            self.test_loader = self.data_loader['test']
            self.train_epoch()
            if self.epoch % self.log_summary_interval == 0:
                #self.test_epoch()
                #self.testdataset.update_cache()
                #self.log_summary_writer()
                pass
            self.lr_scheduler.step()
            self.dataset.update_cache()
            if self.epoch % 5 == 0:
                try:
                    self.plot_latent_space(self.epoch)
                except Exception as e:
                    print(f"Error plotting latent space: {e}")
        
        self.dataset.shutdown()
        self.testdataset.shutdown()
        self.total_progress_bar.write('Finish training')
        self.save_model(self.save_name)
        reconstruction_model_path = self.save_name.replace('.pth', '_reconstruction.pth')
        self.save_reconstruction_model(reconstruction_model_path)
        self.create_gif()
        self.plot_losses()
        return self.acc_dict['best_test_acc']

    def train_epoch(self):
        epoch_iterator = tqdm(self.train_loader, desc="Training (X / X Steps) (loss=X.X)", dynamic_ncols=True)
        total_batches = len(self.train_loader)
        running_loss = 0
        
        for step, batch in enumerate(epoch_iterator):
            
            loss,classif_acc = self.train_step(batch)
            running_loss += loss['total_loss'].item()
            average_loss = running_loss / (step + 1)
            epoch_iterator.set_description("Training ({}/ {}) (loss={:.4f}), epoch contrastive loss={:.4f}, epoch classification loss={:.4f}, classif_acc={:.4f}".format(step + 1, total_batches, average_loss,loss['contrast_loss'],loss['classification_loss'],classif_acc))
            epoch_iterator.refresh()
            if step % self.step_interval == 0:
                self.save_losses(average_loss)
        self.total_progress_bar.update(1)
        self.epoch += 1
        
        
    def train_step(self,batch):
        # update the learning rate of the optimizer
        self.optimizer.zero_grad()

        # prepare batch
        imgs_s = batch["image"].cuda()
        all_labels = batch["roi_label"].cuda()
        scanner_labels = batch["scanner_label"].cuda()
        ids = all_labels

        # encoder inference
        latents = self.model.swinViT(imgs_s)
        
        
        #narrow the latents to use the contrastive latent space (maybe pass to encoder10 for latents[4] before contrastive loss ?)
        nlatents4, bottleneck = torch.split(latents[4], [self.contrastive_latentsize, latents[4].size(1) - self.contrastive_latentsize], dim=1)
        nlatents = [latents[0], latents[1], latents[2], latents[3],0]
        nlatents[4] = nlatents4
        #print("bottleneck size",bottleneck.size())
        #print("nlatents[4] size",nlatents[4].size())
        
        #print("ids size",ids.size())
        #self.contrastive_step(nlatents,ids,latentsize = self.contrastive_latentsize)
        #print(f"Contrastive Loss: {self.losses_dict['contrast_loss']}")
        
        #features = torch.mean(bottleneck, dim=(2, 3, 4))
        #accu = self.classification_step(features, scanner_labels)
        #print(f"Train Accuracy: {accu}%")
        accu = 0
        self.losses_dict['classification_loss'] = 0.0
        
        
        #image reconstruction (either segmentation using the decoder or straight reconstruction using a deconvolution)
        reconstructed_imgs = self.reconstruct_image(latents[4]) 
        
        #saving nifti image to disk
        img = reconstructed_imgs[0,:,:,:,:].detach().cpu().numpy()
        img = np.squeeze(img)
        img = nib.Nifti1Image(img, np.eye(4))
        nib.save(img, "reconstructed_image.nii")
        
        #saving original image to disk
        img = imgs_s[0,:,:,:,:].detach().cpu().numpy()
        img = np.squeeze(img)
        img = nib.Nifti1Image(img, np.eye(4))
        nib.save(img, "original_image.nii")
        
        
        self.reconstruction_step(reconstructed_imgs, imgs_s) 
        #self.losses_dict['reconstruction_loss'] = 0.0

        if self.epoch >= 0:
            self.losses_dict['total_loss'] = \
            self.losses_dict['classification_loss'] + self.losses_dict['contrast_loss'] + self.losses_dict['reconstruction_loss']
        else:
            self.losses_dict['total_loss'] = self.losses_dict['contrast_loss']

        self.losses_dict['total_loss'].backward()
        self.optimizer.step()

        
        return self.losses_dict, accu

    def classification_step(self, features, labels):
        #print(f"the labels is {labels}")
        if self.classifier is None:
            self.classifier = self.autoclassifier(features.size(1), 13)
        logits = self.classifier(features)
        #print(f"the logits is {logits}")
        classification_loss = self.classification_loss(logits, labels)
        self.losses_dict['classification_loss'] = classification_loss
        
        return compute_accuracy(logits, labels, acc_metric=self.acc_metric)

    def contrastive_step(self, latents,ids,latentsize = 768): #actuellement la loss contrastive est aussi calculé entre sous patchs de la même image, on voudrait eviter ça idealement
        #print("ids",ids)
        
        total_num_elements = latents[4].shape[0] * latents[4].shape[2] * latents[4].shape[3] * latents[4].shape[4]
        all_embeddings = torch.empty(total_num_elements, latentsize)
        all_labels = torch.empty(total_num_elements, dtype=torch.long)
        
        offset = 0
        start_idx = 0
        for id in torch.unique(ids):
            #print("id",id)
            boolids = (ids == id)
            #print("boolids",boolids)
            
            #bottleneck
            #print("latents size",len(latents))
            btneck = latents[4]  # (batch_size, latentsize, D, H, W)
            #print("btneck size",btneck.size())
            btneck = btneck[boolids]
            #print("new btneck size",btneck.size())
            num_elements = btneck.shape[2] * btneck.shape[3] * btneck.shape[4]
            #print("num_elements",num_elements)
        
            # (nbatch_size, 768,D, H, W) -> (nbatch_size * num_elements, latentsize)
            embeddings = btneck.permute(0, 2, 3, 4, 1).reshape(-1, latentsize)
            
            #contrast_ind = torch.arange(offset,offset+num_elements) #with this one under patch of the cropped ROI patch will be compared to each other : negatives within same roi
            contrast_ind = torch.full((num_elements,), offset) #negatives only between different r
            labels = contrast_ind.repeat(btneck.shape[0]) 
            #print("weigth",weigth)
            #print("embeddings size",embeddings.size())
            #print("labels size",labels.size())           
            end_idx = start_idx + embeddings.shape[0]
            all_embeddings[start_idx:end_idx, :] = embeddings
            all_labels[start_idx:end_idx] = labels
            start_idx = end_idx
            
            offset += num_elements
        
        llss = (self.contrast_loss(all_embeddings, all_labels))
        self.losses_dict['contrast_loss'] = llss
        
    def reconstruction_step(self, reconstructed_imgs, original_imgs): 
        reconstruction_loss = self.recons_loss(reconstructed_imgs, original_imgs)
        self.losses_dict['reconstruction_loss'] = reconstruction_loss
        
    def reconstruct_image(self,bottleneck): 
        _, c, h, w, d = bottleneck.shape
        x_rec = bottleneck.flatten(start_dim=2, end_dim=4)
        x_rec = x_rec.view(-1, c, h, w, d)
        x_rec = self.reconstruct(x_rec)
        
        return x_rec
        
    def test_epoch(self):
        self.model.eval()
        total_test_accuracy = []
        with torch.no_grad():
            testing_iterator = tqdm(self.test_loader, desc="Testing (X / X Steps) (loss=X.X)", dynamic_ncols=True)
            running_val_loss = 0
            for step,batch in enumerate(testing_iterator):
                imgs_s = batch["image"].cuda()
                all_labels = batch["roi_label"].cuda()
                #logits = self.classifier(torch.mean(self.model.swinViT(imgs_s)[4], dim=(2, 3, 4)))
                #test_accuracy = compute_accuracy(logits, all_labels, acc_metric=self.acc_metric)
                test_accuracy = 0
                total_test_accuracy.append(test_accuracy)
                running_val_loss += self.losses_dict['total_loss'].item()
                testing_iterator.set_description(f"Testing ({step + 1}/{len(self.test_loader)}) (accuracy={test_accuracy:.4f})")
        avg_test_accuracy = np.mean(total_test_accuracy)
        avg_val_loss = running_val_loss / len(self.test_loader)
        self.acc_dict['best_test_acc'] = avg_test_accuracy
        #self.save_losses(self.train_losses[-1] if self.train_losses else None, avg_val_loss)  # Save the validation loss
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
        
    def save_reconstruction_model(self, path):
        torch.save(self.reconstruct.state_dict(), path)
        print(f'Model weights saved to {path}')

    def plot_latent_space(self, epoch):
        self.model.eval() 
        latents = []
        labels = []  

        with torch.no_grad():
            for batch in self.data_loader['train']:
                images = batch['image'].cuda()
                latents_tensor = self.model.swinViT(images)[4]
                
                batch_size, channels, *dims = latents_tensor.size()
                flatten_size = torch.prod(torch.tensor(dims)).item()
                
                latents_tensor = latents_tensor.reshape(batch_size, channels * flatten_size)
                latents.extend(latents_tensor.cpu().numpy())
                labels.extend(batch['roi_label'].cpu().numpy()) 

        latents_2d = perform_tsne(latents)


        plt.figure(figsize=(10, 10))
        scatter = plt.scatter(latents_2d[:, 0], latents_2d[:, 1], c=labels, cmap='viridis')
        plt.colorbar(scatter, label='Labels')
        plt.title('Latent Space t-SNE')
        plt.xlabel('Dimension 1')
        plt.ylabel('Dimension 2')

        plot_path =  f'{self.save_name}_latent_space_tsne_epoch_{epoch}.png'
        print(f'Saving latent space plot to {plot_path}')
        plt.savefig(plot_path)
        plt.close()  

        self.tsne_plots.append(plot_path)

    def create_gif(self):
        images = []
        for plot_path in self.tsne_plots:
            images.append(imageio.imread(plot_path))
        imageio.mimsave(self.save_name+'_latent_space_evolution.gif', images, duration=1)
    
def compute_accuracy(logits, true_labels, acc_metric='total_mean', print_result=False): #a revoir
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
    

   
    
def group_data(data_list, mode='scanner'):
    group_map = {}

    # Helper function to extract base description for 'repetition' mode
    def extract_base(description):
        base = re.match(r"(.+)(-\s#\d+)$", description)
        if base:
            return base.group(1).strip()
        return description

    group_ids = []  
    for item in data_list:
        series_description = item['info']['SeriesDescription']
        if mode == 'scanner':
            group_key = series_description[:2]
        elif mode == 'repetition':
            group_key = extract_base(series_description)
        
        if group_key not in group_map:
            group_map[group_key] = len(group_map)
        
        item['group_id'] = group_map[group_key]
        group_ids.append(item['group_id']) 
    print("Groups correspondance", group_map)
    return np.array(group_ids)

def create_datasets(data_list, test_size=0.2, seed=42):
    
    if test_size <= 0.00000001:
        return data_list, []
    
    groups = group_data(data_list, mode='scanner') 
    
    
    splitter = GroupShuffleSplit(n_splits=1, test_size=test_size, random_state=seed)
    train_idx, test_idx = next(splitter.split(data_list, groups=groups))

    train_data = [data_list[i] for i in train_idx]
    test_data = [data_list[i] for i in test_idx]
   
    print(f"Number of training samples: {len(train_data)}")
    print(f"Number of testing samples: {len(test_data)}")
    train_groups = np.unique(groups[train_idx])
    val_groups = np.unique(groups[test_idx])
    print(f'Training groups: {train_groups}')
    print(f'Validation groups: {val_groups}')

    return train_data, test_data


class EncodeLabels(Transform):
    def __init__(self, encoder, key='roi_label'):
        self.encoder = encoder
        self.key = key

    def __call__(self, data):
        data[self.key] = self.encoder.transform([data[self.key]])[0]  # Encode the label
        return data

class DebugTransform2(Transform):
    def __call__(self, data):
        print("Image shape:", data['image'].shape)
        print("Encoded label:", data['roi_label'], "Type:", type(data['roi_label']))
        # Optionally, check the unique values in the label if it's a segmentation map
        #if isinstance(data['roi_label'], np.ndarray):
        #    print("Unique values in label:", np.unique(data['roi_label']))
        return data

class ExtractScannerLabel(Transform):
    def __call__(self, data):
        data['scanner_label'] = data['info']['SeriesDescription'][:2]
        return data

class PrintDebug(Transform):
    def __call__(self, data):
        print("Debugging")
        return data
    

def count_parameters(model):
    return sum(p.numel() for p in model.parameters())

def main():
    from sklearn.preprocessing import LabelEncoder
    device = Train.get_device(None)
    labels = ['normal1', 'normal2', 'cyst1', 'cyst2', 'hemangioma', 'metastatsis']
    scanner_labels = ['A1', 'A2', 'B1', 'B2', 'C1', 'D1', 'E1', 'E2', 'F1', 'G1', 'G2', 'H1', 'H2']
    encoder = LabelEncoder()
    encoder.fit(labels)
    scanner_encoder = LabelEncoder()
    scanner_encoder.fit(scanner_labels)
    transforms = Compose([
        #PrintDebug(),
        LoadImaged(keys=["image"]),
        #DebugTransform2(),
        EnsureChannelFirstd(keys=["image"]),
        EnsureTyped(keys=["image"], device=device, track_meta=False),
        EncodeLabels(encoder=encoder),
        ExtractScannerLabel(),
        EncodeLabels(encoder=scanner_encoder, key='scanner_label'),
        #DebugTransform(),
        #DebugTransform2(),
        
    ])

    jsonpath = "./dataset_info_cropped.json"
    data_list = load_data(jsonpath)
    train_data, test_data = create_datasets(data_list,test_size=0.00)
    model = get_model(target_size=(64, 64, 32))
    
    train_dataset = SmartCacheDataset(data=train_data, transform=transforms,cache_rate=1,progress=True,num_init_workers=8, num_replace_workers=8,replace_rate=0.1)
    test_dataset = SmartCacheDataset(data=test_data, transform=transforms,cache_rate=0.15,progress=True,num_init_workers=8, num_replace_workers=8)
    
    train_loader = ThreadDataLoader(train_dataset, batch_size=64, shuffle=True,collate_fn=custom_collate_fn)
    test_loader = ThreadDataLoader(test_dataset, batch_size=12, shuffle=False,collate_fn=custom_collate_fn)
    
    data_loader = {'train': train_loader, 'test': test_loader}
    dataset = {'train': train_dataset, 'test': test_dataset}
    
    
    
    print(f"Le nombre total de poids dans le modèle est : {count_parameters(model)}")
    
    optimizer = optim.AdamW(model.parameters(), lr=5e-4, weight_decay=0.005) #i didnt add the decoder params so they didnt get updated
    lr_scheduler = CosineAnnealingLR(optimizer, T_max=50, eta_min=1e-6)
    
    trainer = Train(model, data_loader, optimizer, lr_scheduler, 65,dataset,contrastive_latentsize=700,savename="FT_whole_RECONSTRUCTION_model.pth")
    trainer.train()

if __name__ == '__main__':
    main()
