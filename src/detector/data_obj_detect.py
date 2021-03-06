from collections import defaultdict
import configparser
import csv
from email.policy import default
import os
import os.path as osp
import pickle
from tqdm import tqdm, trange
from PIL import Image
import numpy as np
import scipy
import torch
import torchvision
import torchvision.transforms.functional as TF
import pandas as pd

from src.detector.utils import mask_convert
from src.detector.data_utils import decode_segmentation
from src.tracker.data_track import split_sequence_names
from src.utils.file_utils import listdir_nohidden
from src.tracker.data_track import load_detection_from_txt


class MOT16ObjDetect(torch.utils.data.Dataset):
    """ Data class for the Multiple Object Tracking Dataset

    Problem: the cv3dst chair messed up, some boxes and masks dont match. There are boxes without mask and masks without boxes.
    - solution 1: (implemented)
        if only_obj_w_mask: 
            return only objects with existing masks and boxes 
        else
            return all boxes. if box has a mask, return mask, else return dummy mask

    - solution 2: if mask without box -> create box from mask extreme points (todo)
    - solution 3: if box without max -> create mask by filling out entire box (todo)
    - solution 2 and 3 can be combined  (todo)

    """

    def __init__(
        self,
        root,
        split,
        sparse_version=False,
        transforms=None,
        vis_threshold=0.25,
        segmentation=False,
        only_obj_w_mask=True,
    ):
        self.root = root
        self.split = split
        self.transforms = transforms
        self._segmentation = segmentation
        self._vis_threshold = vis_threshold
        self._only_obj_w_mask = only_obj_w_mask
        self._classes = ("background", "pedestrian")
        self._img_paths = []
        self._seg_paths = []

        self._train_folders = listdir_nohidden(os.path.join(root, "train"))
        self._test_folders = listdir_nohidden(os.path.join(root, "test"))

        seq_names = split_sequence_names(split, root)
        sparse_frequency = 20
        img_index = 0
        self._convert_frame_to_img_idx = {
            seq_name: {} for seq_name in seq_names
        }
        for seq_name in seq_names:
            if seq_name in self._train_folders:
                path = os.path.join(root, "train", seq_name)
            else:
                path = os.path.join(root, "test", seq_name)

            config_file = os.path.join(path, "seqinfo.ini")

            assert os.path.exists(
                config_file
            ), "Path does not exist: {}".format(config_file)

            config = configparser.ConfigParser()
            config.read(config_file)
            seq_len = int(config["Sequence"]["seqLength"])
            im_ext = config["Sequence"]["imExt"]
            im_dir = config["Sequence"]["imDir"]

            self._imDir = os.path.join(path, im_dir)
            self._seg_dir = os.path.join(path, "seg_ins")

            if os.path.exists(self._seg_dir):
                for frame_id, seg_file in enumerate(
                    os.listdir(self._seg_dir), start=1
                ):
                    # for i in range(1, seq_len + 1):
                    if not sparse_version or (
                        sparse_version and frame_id % sparse_frequency == 0
                    ):
                        # seg_path = os.path.join(self._seg_dir, f"{i:06d}.png")
                        seg_path = os.path.join(self._seg_dir, seg_file)
                        self._seg_paths.append(seg_path)

            for frame_id, img_file in enumerate(
                os.listdir(self._imDir), start=1
            ):
                # for i in range(1, seq_len + 1):
                if not sparse_version or (
                    sparse_version and frame_id % sparse_frequency == 0
                ):
                    # img_path = os.path.join(self._imDir, f"{i:06d}{im_ext}")
                    img_path = os.path.join(self._imDir, img_file)
                    assert os.path.exists(
                        img_path
                    ), "Path does not exist: {img_path}"
                    # self._img_paths.append((img_path, im_width, im_height))
                    self._img_paths.append(img_path)
                    self._convert_frame_to_img_idx[seq_name][
                        frame_id
                    ] = img_index
                    img_index += 1

    @property
    def num_classes(self):
        return len(self._classes)

    def _get_annotation(self, idx):
        """
        """
        img_path = self._img_paths[idx]
        file_index = int(os.path.basename(img_path).split(".")[0])

        gt_file = os.path.join(
            os.path.dirname(os.path.dirname(img_path)), "gt", "gt.txt"
        )

        assert os.path.exists(gt_file), "GT file does not exist: {}".format(
            gt_file
        )

        gt = load_detection_from_txt(
            gt_file, vis_threshold=self._vis_threshold, mode="gt"
        )
        boxes = gt["boxes"][file_index]
        visibilities = gt["visibilities"][file_index]

        # - ids of objects for which bounding boxes are available
        # - it can be the case that more boxes than masks are available
        # - we need to create padded masks for those masks, so that the index of masks matches the index of boxes
        # - padding will be all zeros and removed later again
        if self._segmentation:
            masks, keep_ids = load_segmentation(
                seg_path=self._seg_paths[idx],
                box_ids=torch.Tensor(boxes.keys()),
                only_obj_with_mask=True,
            )
            boxes = {id: vis for (id, vis) in boxes.items() if id in keep_ids}
            visibilities = {
                id: vis for (id, vis) in visibilities.items() if id in keep_ids
            }

        num_objs = len(boxes.keys())
        boxes = torch.stack(list(boxes.values()), axis=0)
        visibilities = torch.Tensor(list(visibilities.values()))

        sample = {
            "boxes": boxes,
            "labels": torch.ones((num_objs,), dtype=torch.int64),
            "image_id": torch.tensor([idx]),
            "area": torchvision.ops.box_area(boxes),
            "iscrowd": torch.zeros((num_objs,), dtype=torch.int64),
            "visibilities": visibilities,
        }
        if self._segmentation:
            sample.update({"masks": masks})
        return sample

    def __getitem__(self, idx):
        # load images ad masks
        img_path = self._img_paths[idx]

        # mask_path = os.path.join(self.root, "PedMasks", self.masks[idx])
        img = Image.open(img_path).convert("RGB")

        target = self._get_annotation(idx)

        if self.transforms is not None:
            img, target = self.transforms(img, target)

        return img, target

    def __len__(self):
        return len(self._img_paths)

    def write_results_files(self, results, output_dir):
        """Write the detections in the format for MOT17Det sumbission

        all_boxes[image] = N x 5 array of detections in (x1, y1, x2, y2, score)

        Each file contains these lines:
        <frame>, <id>, <bb_left>, <bb_top>, <bb_width>, <bb_height>, <conf>, <x>, <y>, <z>

        Files to sumbit:
        ./MOT17-01.txt
        ./MOT17-02.txt
        ./MOT17-03.txt
        ./MOT17-04.txt
        ./MOT17-05.txt
        ./MOT17-06.txt
        ./MOT17-07.txt
        ./MOT17-08.txt
        ./MOT17-09.txt
        ./MOT17-10.txt
        ./MOT17-11.txt
        ./MOT17-12.txt
        ./MOT17-13.txt
        ./MOT17-14.txt
        """

        # format_str = "{}, -1, {}, {}, {}, {}, {}, -1, -1, -1"

        files = {}
        for image_id, res in results.items():
            path = self._img_paths[image_id]
            img1, name = osp.split(path)
            # get image number out of name
            frame = int(name.split(".")[0])
            # smth like /train/MOT17-09-FRCNN or /train/MOT17-09
            tmp = osp.dirname(img1)
            # get the folder name of the sequence and split it
            tmp = osp.basename(tmp).split("-")
            # Now get the output name of the file
            out = tmp[0] + "-" + tmp[1] + ".txt"
            outfile = osp.join(output_dir, out)

            # check if out in keys and create empty list if not
            if outfile not in files.keys():
                files[outfile] = []

            for box, score in zip(res["boxes"], res["scores"]):
                x1 = box[0].item()
                y1 = box[1].item()
                x2 = box[2].item()
                y2 = box[3].item()
                files[outfile].append(
                    [
                        frame,
                        -1,
                        x1,
                        y1,
                        x2 - x1,
                        y2 - y1,
                        score.item(),
                        -1,
                        -1,
                        -1,
                    ]
                )

        for k, v in files.items():
            with open(k, "w") as of:
                writer = csv.writer(of, delimiter=",")
                for d in v:
                    writer.writerow(d)

    def compare_detectors(self, results1, results2, ovthresh=0.5):
        all_sequences_compare_results = defaultdict(dict)
        for (seq_name, seq_results1), (_, seq_results2) in zip(
            results1.items(), results2.items()
        ):
            npos = 0
            for frame_id, (frame_result1, frame_result2) in enumerate(
                zip(seq_results1, seq_results2), start=1
            ):
                im_index = self._convert_frame_to_img_idx[seq_name][frame_id]
                annotation = self._get_annotation(im_index)

                visible = annotation["visibilities"] > self._vis_threshold
                npos += len(visible)
                im_gt = annotation["boxes"][visible].cpu().numpy()
                im_det1 = frame_result1["boxes"].cpu().numpy()
                im_det2 = frame_result2["boxes"].cpu().numpy()
                
                found1, im_tp1, im_fp1 = tp_and_fp_of_detection(
                    im_gt=im_gt, im_det=im_det1, ovthresh=ovthresh
                )

                found2, im_tp2, im_fp2 = tp_and_fp_of_detection(
                    im_gt=im_gt, im_det=im_det2, ovthresh=ovthresh
                )
                tp_exclusive_1 = np.where(
                    np.logical_and(
                        found1.astype("bool"), ~found2.astype("bool")
                    )
                )[0]
                tp_exclusive_2 = np.where(
                    np.logical_and(
                        found2.astype("bool"), ~found1.astype("bool")
                    )
                )[0]

                if tp_exclusive_1.any() or tp_exclusive_2.any():
                    frame_compare_dict = defaultdict(dict)
                    frame_compare_dict["TP"]["exclusive_1"] = tp_exclusive_1
                    frame_compare_dict["TP"]["exclusive_2"] = tp_exclusive_2
                    all_sequences_compare_results[seq_name][
                        frame_id
                    ] = frame_compare_dict
        return all_sequences_compare_results

    def evaluate_detections_on_tracking_data(self, results, ovthresh=0.5):
        """
        results[seq_name] = list(frame["boxes"])
        """
        detector_eval_dict = defaultdict(dict)

        for seq_name, seq_results in results.items():
            tp = [[] for _ in range(len(self._img_paths))]
            fp = [[] for _ in range(len(self._img_paths))]
            npos = 0
            for frame_id, frame_result in enumerate(seq_results, start=1):
                im_index = self._convert_frame_to_img_idx[seq_name][frame_id]
                annotation = self._get_annotation(im_index)

                visible = annotation["visibilities"] > self._vis_threshold
                npos += len(visible)
                im_gt = annotation["boxes"][visible].cpu().numpy()
                im_det = frame_result["boxes"].cpu().numpy()

                found, im_tp, im_fp = tp_and_fp_of_detection(
                    im_gt=im_gt, im_det=im_det, ovthresh=ovthresh
                )
                tp[im_index] = im_tp
                fp[im_index] = im_fp
            seq_eval_dict = detection_metrics_from_tp_and_fp(
                tp=tp, fp=fp, npos=npos
            )
            detector_eval_dict[seq_name] = seq_eval_dict
        return pd.DataFrame(detector_eval_dict).T

    def evaluate_detections(self, results, ovthresh=0.5):
        """Evaluates the detections (not official!!)

        results[im_index]["boxes"], boxes in (x1, y1, x2, y2)
        """

        # Lists for tp and fp in the format tp[cls][image]
        tp = [[] for _ in range(len(self._img_paths))]
        fp = [[] for _ in range(len(self._img_paths))]
        npos = 0

        for im_index in list(results.keys()):
            annotation = self._get_annotation(im_index)
            visible = annotation["visibilities"] > self._vis_threshold
            npos += len(visible)
            im_gt = annotation["boxes"][visible].cpu().numpy()
            im_det = results[im_index]["boxes"].cpu().numpy()

            found, im_tp, im_fp = tp_and_fp_of_detection(
                im_gt=im_gt, im_det=im_det, ovthresh=ovthresh
            )
            tp[im_index] = im_tp
            fp[im_index] = im_fp
        detector_eval_dict = detection_metrics_from_tp_and_fp(
            tp=tp, fp=fp, npos=npos
        )
        return detector_eval_dict


def tp_and_fp_of_detection(im_gt, im_det, ovthresh=0.5):
    found = np.zeros(len(im_gt))
    im_tp = np.zeros(len(im_det))
    im_fp = np.zeros(len(im_det))
    for i, d in enumerate(im_det):
        ovmax = -np.inf
        if im_gt.size > 0:
            ixmin = np.maximum(im_gt[:, 0], d[0])
            iymin = np.maximum(im_gt[:, 1], d[1])
            ixmax = np.minimum(im_gt[:, 2], d[2])
            iymax = np.minimum(im_gt[:, 3], d[3])
            iw = np.maximum(ixmax - ixmin + 1.0, 0.0)
            ih = np.maximum(iymax - iymin + 1.0, 0.0)
            inters = iw * ih

            uni = (
                (d[2] - d[0] + 1.0) * (d[3] - d[1] + 1.0)
                + (im_gt[:, 2] - im_gt[:, 0] + 1.0)
                * (im_gt[:, 3] - im_gt[:, 1] + 1.0)
                - inters
            )

            overlaps = inters / uni
            ovmax = np.max(overlaps)
            jmax = np.argmax(overlaps)

        if ovmax > ovthresh:
            if found[jmax] == 0:
                im_tp[i] = 1.0
                found[jmax] = 1.0
            else:
                im_fp[i] = 1.0
        else:
            im_fp[i] = 1.0
    return found, im_tp, im_fp


def detection_metrics_from_tp_and_fp(tp, fp, npos):
    # Flatten out tp and fp into a numpy array
    i = 0
    for im in tp:
        if type(im) != type([]):
            i += im.shape[0]

    tp_flat = np.zeros(i)
    fp_flat = np.zeros(i)

    i = 0
    for tp_im, fp_im in zip(tp, fp):
        if type(tp_im) != type([]):
            s = tp_im.shape[0]
            tp_flat[i : s + i] = tp_im
            fp_flat[i : s + i] = fp_im
            i += s

    tp = np.cumsum(tp_flat)
    fp = np.cumsum(fp_flat)
    if len(tp) == 0:
        detector_eval_dict = {
            "AP": 0.0,
            "Prec": 0.0,
            "Rec": 0.0,
            "TP": 0,
            "FP": 0,
        }
        return detector_eval_dict

    rec = tp / float(npos)
    # avoid divide by zero in case the first detection matches a difficult
    # ground truth (probably not needed in my code but doesn't harm if left)
    prec = tp / np.maximum(tp + fp, np.finfo(np.float64).eps)
    tmp = np.maximum(tp + fp, np.finfo(np.float64).eps)

    # correct AP calculation
    # first append sentinel values at the end
    mrec = np.concatenate(([0.0], rec, [1.0]))
    mpre = np.concatenate(([0.0], prec, [0.0]))

    # compute the precision envelope
    for i in range(mpre.size - 1, 0, -1):
        mpre[i - 1] = np.maximum(mpre[i - 1], mpre[i])

    # to calculate area under PR curve, look for points
    # where X axis (recall) changes value
    i = np.where(mrec[1:] != mrec[:-1])[0]

    # and sum (\Delta recall) * prec
    ap = np.sum((mrec[i + 1] - mrec[i]) * mpre[i + 1])

    detector_eval_dict = {
        "AP": f"{ap.item():.2f}",
        "Prec": f"{prec[-1].item():.2f}",
        "Rec": f"{np.max(rec).item():.2f}",
        "TP": str(int(np.max(tp).item())),
        "FP": str(int(np.max(fp).item())),
    }
    return detector_eval_dict

