import tensorflow as tf
import tf2onnx
import torch
from onnx2pytorch import ConvertModel
import onnx
import os
import numpy as np
import csv
from tqdm import tqdm
from monai.transforms import Compose, LoadImaged, EnsureChannelFirstd, EnsureTyped
from monai.data import SmartCacheDataset, ThreadDataLoader

from qa4iqi_extraction.constants import MANUFACTURER_FIELD, MANUFACTURER_MODEL_NAME_FIELD, SERIES_DESCRIPTION_FIELD, SERIES_NUMBER_FIELD, SLICE_THICKNESS_FIELD
from swin_contrastive.swinunetr import custom_collate_fn, load_data

def convert_tf_to_pytorch():
    tf.disable_v2_behavior()
    sess = tf.Session()
    saver = tf.train.import_meta_graph('organs-5c-30fs-acc92-121.meta')
    saver.restore(sess, tf.train.latest_checkpoint('./'))

    graph = tf.get_default_graph()
    x = graph.get_tensor_by_name("x_start:0")
    keepProb = graph.get_tensor_by_name("keepProb:0")
    feature_tensor = graph.get_tensor_by_name('MaxPool3D_1:0')

    onnx_model, _ = tf2onnx.convert.from_session(sess, input_names=['x_start:0', 'keepProb:0'], output_names=['MaxPool3D_1:0'])
    onnx.save(onnx_model, "tf_model.onnx")

    pytorch_model = ConvertModel(onnx.load("tf_model.onnx"))
    torch.save(pytorch_model.state_dict(), 'pytorch_model.pth')

    return pytorch_model

def run_inference():
    jsonpath = "./dataset_info_cropped.json"
    device_id = 0
    os.environ["CUDA_VISIBLE_DEVICES"] = str(device_id)
    torch.cuda.set_device(device_id)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # Convert and load the PyTorch model
    pytorch_model = convert_tf_to_pytorch()
    pytorch_model.to(device)
    pytorch_model.eval()

    transforms = Compose([
        LoadImaged(keys=["image"]),
        EnsureChannelFirstd(keys=["image"]),
        EnsureTyped(keys=["image"], device=device, track_meta=False),
    ])

    datafiles = load_data(jsonpath)
    dataset = SmartCacheDataset(data=datafiles, transform=transforms, cache_rate=1, progress=True, num_init_workers=8, num_replace_workers=8, replace_rate=0.1)
    print("dataset length: ", len(datafiles))
    dataload = ThreadDataLoader(dataset, batch_size=1, collate_fn=custom_collate_fn)

    with open("torch_normalized_deepfeaturesoscar.csv", "w", newline="") as csvfile:
        fieldnames = ["SeriesNumber", "deepfeatures", "ROI", "SeriesDescription", "ManufacturerModelName", "Manufacturer", "SliceThickness"]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        dataset.start()

        for batch in tqdm(dataload):
            with torch.no_grad():
                image = batch["image"].view(-1, 1, 64, 64, 32).to(device)
                features = pytorch_model(image)
                latentrep = features.cpu().numpy().reshape(-1).tolist()

            record = {
                "SeriesNumber": batch["info"][SERIES_NUMBER_FIELD][0],
                "deepfeatures": latentrep,
                "ROI": batch["roi_label"][0],
                "SeriesDescription": batch["info"][SERIES_DESCRIPTION_FIELD][0],
                "ManufacturerModelName": batch["info"][MANUFACTURER_MODEL_NAME_FIELD][0],
                "Manufacturer": batch["info"][MANUFACTURER_FIELD][0],
                "SliceThickness": batch["info"][SLICE_THICKNESS_FIELD][0],
            }
            writer.writerow(record)
        
        dataset.shutdown()

    print("Done!")

if __name__ == "__main__":
    run_inference()