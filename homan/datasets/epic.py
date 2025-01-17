#!/usr/bin/env python
# -*- coding: utf-8 -*-

import cv2
import re
import numpy as np
import torch
from PIL import Image
from tqdm import tqdm
from typing import Union
from pathlib import Path
import bisect

from homan.datasets import collate, epichoa, tarutils
from homan.datasets.chunkvids import chunk_vid_index
from homan.tracking import trackhoa as trackhoadf
from homan.utils import bbox as bboxutils

import os
import pandas as pd
import pickle, json
import trimesh
import warnings
from libyana.lib3d import kcrop
from libyana.transformutils import handutils
from manopth import manolayer

OBJ_ROOT="/home/skynet/Zhifan/ihoi/weights/obj_models/"
MODELS = {
    "bottle": {
        "path": OBJ_ROOT + "bottle_500.obj",
        "scale": 0.2,
    },
    "bowl": {
        "path": OBJ_ROOT + "bowl_500.obj",
        "scale": 0.2,
    },
    # "jug": {
    #     "path":
    #     "local_data/datasets/ho3dv2/processmodels/019_pitcher_base/textured_simple_400.obj",
    #     "scale": 0.25,
    # },
    # "pitcher": {
    #     "path":
    #     "local_data/datasets/ho3dv2/processmodels/019_pitcher_base/textured_simple_400.obj",
    #     "scale": 0.25,
    # },
    "plate": {
        "path": OBJ_ROOT + "plate_500.obj",
        "scale": 0.3,
    },
    "mug": {
        "path": OBJ_ROOT + "mug_1000.obj",
        "scale": 0.12,
    },
    "cup": {
        "path": OBJ_ROOT + "cup_1000.obj",
        "scale": 0.12,
    },
    # "phone": {
    #     "path": "/gpfsscratch//rech/tan/usk19gv/datasets/ShapeNetCore.v2/"
    #     "02992529/7ea27ed05044031a6fe19ebe291582/models/model_normalized_proc.obj",
    #     "scale": 0.07
    # },
    "can": {
        "path": OBJ_ROOT + "can_500.obj",
        "scale": 0.2
    }
}


def apply_bbox_transform(bbox, affine_trans):
    x_min, y_min = handutils.transform_coords(
        [bbox[:2]],
        affine_trans,
    )[0]
    x_max, y_max = handutils.transform_coords(
        [bbox[2:]],
        affine_trans,
    )[0]
    new_bbox = np.array([x_min, y_min, x_max, y_max])
    return new_bbox


def load_models(MODELS, normalize=True):
    models = {}
    for obj_name, obj_info in MODELS.items():
        obj_path = obj_info["path"]
        scale = obj_info["scale"]
        obj = trimesh.load(obj_path)
        verts = np.array(obj.vertices)
        if normalize:
            # center
            verts = verts - verts.mean(0)
            # inscribe in 1-radius sphere
            verts = verts / np.linalg.norm(verts, 2, 1).max() * scale / 2
        models[obj_name] = {
            "verts": verts,
            "faces": np.array(obj.faces),
            "path": obj_path,
        }
    return models


class PairLocator:
    """ locate a (vid, frame) in P01_01_0003 """
    def __init__(self,
                 result_root='/home/skynet/Zhifan/data/visor-dense/480p',
                 pair_infos='/home/skynet/Zhifan/data/visor-dense/meta_infos/480p_pair_infos.txt',
                 verbose=True):
        self.result_root = Path(result_root)
        with open(pair_infos) as fp:
            pair_infos = fp.readlines()
            pair_infos = [v.strip().split(' ') for v in pair_infos]

        self._build_index(pair_infos)
        self.verbose = verbose

    def _build_index(self, pair_infos: list):
        """ pair_infos[i] = ['P01_01_0003', '123', '345']
        """
        self._all_full_frames = []
        self._all_folders = []
        for folder, st, ed in pair_infos:
            min_frame = int(st)
            index = self._hash(folder, min_frame)
            self._all_full_frames.append(index)
            self._all_folders.append(folder)

        self._all_full_frames = np.int64(self._all_full_frames)
        sort_idx = np.argsort(self._all_full_frames)
        self._all_full_frames = self._all_full_frames[sort_idx]
        self._all_folders = np.asarray(self._all_folders)[sort_idx]

    @staticmethod
    def _hash(vid: str, frame: int):
        pid, sub = vid.split('_')[:2]
        pid = pid[1:]
        op1, op2, op3 = map(int, (pid, sub, frame))
        index = op1 * int(1e15) + op2 * int(1e12) + op3
        return index

    def __call__(self, vid, frame):
        return self.locate(vid, frame)

    def locate(self, vid, frame) -> Union[str, None]:
        """
        Returns: a str in DAVIS folder format: {vid}_{%4d}
            e.g P11_16_0107
        """
        query = self._hash(vid, frame)
        loc = bisect.bisect_right(self._all_full_frames, query)
        if loc == 0:
            return None
        r = self._all_folders[loc-1]
        r_vid = '_'.join(r.split('_')[:2])
        if vid != r_vid:
            if self.verbose:
                print(f"folder for {vid} not found")
            return None
        frames = map(
            lambda x: int(re.search('[0-9]{10}', x).group(0)),
            os.listdir(self.result_root/r))
        if max(frames) < frame:
            if self.verbose:
                print(f"Not found in {r}")
            return None
        return r


class Epic:
    def __init__(
        self,
        root="local_data/datasets",
        joint_nb=21,
        use_cache=False,
        mano_root="extra_data/mano",
        mode="frame",
        ref_idx=0,
        valid_step=-1,
        frame_step=1,
        frame_nb=10,
        verbs=[  # "take", "put",
            "take", "put"
            "open", "close"
        ],
        nouns=[
            "can",
            "cup",
            # "phone",
            "plate",
            # "pitcher",
            # "jug",
            "bottle",
            "bowl",
            "mug",
        ],
        box_folder="data/boxes",
        track_padding=10,
        min_frame_nb=20,
        epic_root="/media/skynet/DATA/Datasets/epic-100/rgb", # "local_data/datasets/epic",
        epic_static_path="/media/skynet/DATA/Zhifan/epic_analysis/hos/tools/model-input-Feb03.json",
        detections_path="/home/skynet/Zhifan/ihoi/weights/v3_clip_boxes.pkl",
        use_visor_mask=False,
    ):
        """
        Arguments:
            min_frame_nb (int): Only sequences with length at least min_frame_nb are considered
            track_padding (int): Number of frames to include before and after extent of action clip
                provided by the annotations
            frame_step (int): Number of frames to skip between two selected images
            verbs (list): subset of action verbs to consider
            nouns (list): subset of action verbs to consider
        """
        super().__init__()
        self.name = "epic"
        self.mode = mode
        self.object_models = load_models(MODELS)
        # self.frame_template = os.path.join(epic_root, "{}/{}/frame_{:010d}.jpg")
        self.image_fmt = '/media/skynet/DATA/Datasets/visor-dense/480p/%s/%s_frame_%010d.jpg'  # % (folder, vid, frame)
        self.visor_fmt = '/media/skynet/DATA/Datasets/visor-dense/interpolations/%s/%s_frame_%010d.png' # (vid, vid, frame)
        cat_data_mapping = '/media/skynet/DATA/Datasets/visor-dense/meta_infos/data_mapping.json'
        with open(cat_data_mapping, 'r') as fp:
            self.cat_data_mapping = json.load(fp)

        with open(epic_static_path, "r") as f:
            self.clip_infos = json.load(f)
        with open(detections_path, "rb") as f:
            self.detections = pickle.load(f)

        self.resize_factor = 1  # 3
        self.frame_nb = frame_nb
        self.image_size = (640, 360)  # == IMAGE_SIZE
        cache_folder = os.path.join("data", "cache")
        os.makedirs(cache_folder, exist_ok=True)

        self.root = os.path.join(root, self.name)
        left_faces = manolayer.ManoLayer(mano_root="extra_data/mano",
                                         side="left").th_faces.numpy()
        right_faces = manolayer.ManoLayer(mano_root="extra_data/mano",
                                          side="right").th_faces.numpy()
        self.faces = {"left": left_faces, "right": right_faces}

        # annotations, vid_index = self._get_annotatino_vid_index(
        #     use_cache, nouns, min_frame_nb, frame_step)
        self.use_visor_mask = use_visor_mask
        self.locator  = PairLocator()
        annotations, vid_index = self._read_annotations_static(
            min_frame_nb)

        self.annotations = annotations
        self.vid_index = vid_index
        # self.chunk_index = chunk_vid_index(self.vid_index,
        #                                    chunk_size=frame_nb,
        #                                    chunk_step=frame_step,
        #                                    chunk_spacing=frame_step * frame_nb)
        # self.chunk_index = self.chunk_index[self.chunk_index.object.isin(
        #     nouns)]
        # print(f"Working with {len(self.chunk_index)} chunks for {nouns}")

        # Get paired links as neighboured joints
        self.links = [
            (0, 1, 2, 3, 4),
            (0, 5, 6, 7, 8),
            (0, 9, 10, 11, 12),
            (0, 13, 14, 15, 16),
            (0, 17, 18, 19, 20),
        ]

    def _keep_frame_with_boxes(self, vid, start, end, side, cat):
        """
        Returns:
            frame_idxs: frame indices in which both obj and hand box are present
            bboxes: dict
                -objects: (N, 4) left, top, right, bottom
                -{side}_hand: (N, 4) left, top, right, bottom
        """
        vid_boxes = self.detections[vid]
        valid_frames = []
        hand = f"{side}_hand"
        bboxes = {
            "objects": [],
            hand: [],
        }
        for frame in range(start, end+1):
            frame_boxes = vid_boxes[frame]
            if side not in frame_boxes or frame_boxes[side] is None:
                continue
            if cat not in frame_boxes or frame_boxes[cat] is None:
                continue
            valid_frames.append(frame)
            bboxes["objects"].append(frame_boxes[cat])
            bboxes[hand].append(frame_boxes[side])
        if len(bboxes["objects"]) == 0:
            return valid_frames, bboxes
        # detection boxes are xywh
        _obj_bboxes = np.stack(bboxes["objects"], 0)
        _hand_bboxes = np.stack(bboxes[hand], 0)
        _obj_bboxes[:, 2:] += _obj_bboxes[:, :2]
        _hand_bboxes[:, 2:] += _hand_bboxes[:, :2]

        DETECTION_IMG_SIZE = (854, 480)
        IMAGE_SIZE = (640, 360)

        _obj_bboxes = _obj_bboxes / (DETECTION_IMG_SIZE * 2) * (IMAGE_SIZE * 2)
        _hand_bboxes = _hand_bboxes / (DETECTION_IMG_SIZE * 2) * (IMAGE_SIZE * 2)

        bboxes["objects"] = _obj_bboxes
        bboxes[hand] = _hand_bboxes
        return valid_frames, bboxes

    def _get_visor_mask(self, vid, frame, side_id, cid, affine_trans, res):
        """ get visor mask and apply affine_trans to [res, res]
        Args:
            cid: is the `visor_name` id, not `cat`

        Returns:
            mask_hand, mask_obj
        """
        path = self.visor_fmt % (vid, vid, frame)
        mask = Image.open(path).convert('P')
        mask = mask.resize(self.image_size, Image.NEAREST)
        mask = handutils.transform_img(mask, affine_trans, [res, res])
        mask = np.asarray(mask)

        mask_hand = np.zeros_like(mask)
        mask_obj = np.zeros_like(mask)
        mask_hand[mask == side_id] = 1
        mask_obj[mask == cid] = 1
        return mask_hand, mask_obj

    def _read_annotations_static(self, min_frame_nb):
        """
                        vid_index.append({
                            "seq_idx": annot_full_key,
                            "frame_nb": len(frame_idxs),
                            "start_frame": min(frame_idxs),
                            "object": annot.noun,
                            "verb": annot.verb,
                        })
                        annotations[annot_full_key] = {
                            "bboxes_xyxy": bboxes,
                            "frame_idxs": frame_idxs
                        }
                except Exception:
                    print(f"Skipping idx {annot_idx}")
            vid_index = pd.DataFrame(vid_index)
            dataset_annots = {
                "vid_index": vid_index,
                "annotations": annotations,
            }
        """
        annotations = {}
        vid_index = []
        for _, clip_info in enumerate(self.clip_infos):
            frame_idxs, bboxes = self._keep_frame_with_boxes(
                clip_info["vid"], clip_info["start"], clip_info["end"], clip_info["side"],
                clip_info["cat"])
            if len(frame_idxs) > min_frame_nb:
                # annot_key = None
                # annot_idx = clip_idx
                # video_id = None
                # annot_full_key = (video_id, annot_idx, annot_key)
                annot_full_key = "%s_%d_%d" % (
                    clip_info['vid'], clip_info['start'], clip_info['end'])

                cat = clip_info["cat"]
                visor_name = clip_info["visor_name"]
                vid_index.append({
                    "seq_idx": annot_full_key,
                    "frame_nb": len(frame_idxs),
                    "start_frame": min(frame_idxs),

                    "object": cat,
                    "visor_name": visor_name,
                    "side": clip_info["side"],  # 'left' or 'right'

                    # "verb": None,
                })
                annotations[annot_full_key] = {
                    "bboxes_xyxy": bboxes,
                    "frame_idxs": frame_idxs
                }
        vid_index = pd.DataFrame(vid_index)
        return annotations, vid_index

    def _get_annotatino_vid_index(self, use_cache, nouns,
                                  track_padding,
                                  min_frame_nb,
                                  frame_step):
        cache_path = 'data/cache/epic_take_putopen_close_can_cup_phone_plate_pitcher_jug_bottle_20.pkl'
        if os.path.exists(cache_path) and use_cache:
            with open(cache_path, "rb") as p_f:
                dataset_annots = pickle.load(p_f)
            vid_index = dataset_annots["vid_index"]
            annotations = dataset_annots["annotations"]
        else:
            with open("local_data/datasets/epic/EPIC_100_train.pkl",
                      "rb") as p_f:
                annot_df = pickle.load(p_f)

            # annot_df = annot_df[annot_df.video_id.str.len() == 6]

            annot_df = annot_df[annot_df.noun.isin(nouns)]
            print(f"Processing {annot_df.shape[0]} clips for nouns {nouns}")
            vid_index = []
            annotations = {}
            """
            annot_idx
            """
            for annot_idx, (annot_key,
                            annot) in enumerate(tqdm(annot_df.iterrows())):
                try:
                    hoa_dets = epichoa.load_video_hoa(
                        annot.video_id,
                        hoa_root="local_data/datasets/epic/hoa")
                    frame_idxs, bboxes = trackhoadf.track_hoa_df(
                        hoa_dets,
                        video_id=annot.video_id,
                        start_frame=max(1, annot.start_frame - track_padding),
                        end_frame=(min(annot.stop_frame + track_padding,
                                       hoa_dets.frame.max() - 1)),
                        dt=frame_step / 60,
                    )
                    if len(frame_idxs) > min_frame_nb:
                        annot_full_key = (annot.video_id, annot_idx, annot_key)
                        vid_index.append({
                            "seq_idx": annot_full_key,
                            "frame_nb": len(frame_idxs),
                            "start_frame": min(frame_idxs),
                            "object": annot.noun,
                            "verb": annot.verb,
                        })
                        annotations[annot_full_key] = {
                            "bboxes_xyxy": bboxes,
                            "frame_idxs": frame_idxs
                        }
                except Exception:
                    print(f"Skipping idx {annot_idx}")
            vid_index = pd.DataFrame(vid_index)
            dataset_annots = {
                "vid_index": vid_index,
                "annotations": annotations,
            }
            with open(cache_path, "wb") as p_f:
                pickle.dump(dataset_annots, p_f)
        return annotations, vid_index

    def get_roi(self, video_annots, frame_ids, res=640):
        """
        Get square ROI in xyxy format
        """
        # Get all 2d points and extract bounding box with given image
        # ratio
        annots = self.annotations[video_annots.seq_idx]
        bboxes = [bboxs[frame_ids] for bboxs in annots["bboxes_xyxy"].values()]
        all_vid_points = np.concatenate(list(bboxes)) / self.resize_factor
        xy_points = np.concatenate(
            [all_vid_points[:, :2], all_vid_points[:, 2:]], 0)
        mins = xy_points.min(0)
        maxs = xy_points.max(0)
        roi_box_raw = np.array([mins[0], mins[1], maxs[0], maxs[1]])
        roi_bbox = bboxutils.bbox_wh_to_xy(
            bboxutils.make_bbox_square(bboxutils.bbox_xy_to_wh(roi_box_raw),
                                       bbox_expansion=0.2))
        roi_center = (roi_bbox[:2] + roi_bbox[2:]) / 2
        # Assumes square bbox
        roi_scale = roi_bbox[2] - roi_bbox[0]
        affine_trans = handutils.get_affine_transform(roi_center, roi_scale,
                                                      [res, res])[0]
        return roi_bbox, affine_trans

    def __getitem__(self, idx):
        if self.mode == "vid":
            return self.get_vid_info(idx, mode="full_vid")
        elif self.mode == "chunk":
            return self.get_vid_info(idx, mode="chunk")

    def get_vid_info(self, idx, res=640, mode="full_vid"):
        # Use all frames if frame_nb is -1
        if mode == "full_vid":
            vid_info = self.vid_index.iloc[idx]
            # Use all frames if frame_nb is -1
            if self.frame_nb == -1:
                frame_nb = vid_info.frame_nb
            else:
                frame_nb = self.frame_nb

            frame_ids = np.linspace(0, vid_info.frame_nb - 1,
                                    frame_nb).astype(np.int)
        else:
            vid_info = self.chunk_index.iloc[idx]
            frame_ids = vid_info.frame_idxs
        seq_frame_idxs = [self.annotations[vid_info.seq_idx]["frame_idxs"]][0]
        frame_idxs = [seq_frame_idxs[frame_id] for frame_id in frame_ids]
        video_id = vid_info.seq_idx # [0]
        video_id = re.search('P\d{2}_\d{2,3}', video_id)[0]

        side_str = 'left hand' if vid_info.side == 'left' else 'right hand'
        visor_name = vid_info.visor_name
        side_id = self.cat_data_mapping[video_id][side_str]
        cid = self.cat_data_mapping[video_id][visor_name]

        # Read images from tar file
        images = []
        masks_hand, masks_obj = [], []
        seq_obj_info = []
        seq_hand_infos = []
        seq_cameras = []
        roi, affine_trans = self.get_roi(vid_info, frame_ids)
        for frame_id in frame_ids:
            frame_idx = seq_frame_idxs[frame_id]
            folder = self.locator.locate(video_id, frame_idx)
            # img_path = self.frame_template.format("train", video_id[:3],
            #                                       video_id, frame_idx)
            img = cv2.imread(self.image_fmt % (folder, video_id, frame_idx))
            img = cv2.resize(img, self.image_size)
            img = Image.fromarray(img[:, :, ::-1])

            img = handutils.transform_img(img, affine_trans, [res, res])
            images.append(img)

            if self.use_visor_mask:
                mask_hand, mask_obj = self._get_visor_mask(
                    video_id, frame_idx, side_id, cid, affine_trans, res)
                masks_hand.append(mask_hand)
                masks_obj.append(mask_obj)

            obj_info, hand_infos, camera, setup = self.get_hand_obj_info(
                vid_info,
                frame_id,
                roi=roi,
                res=res,
                affine_trans=affine_trans)
            seq_obj_info.append(obj_info)
            seq_hand_infos.append(hand_infos)
            seq_cameras.append(camera)
        hand_nb = len(seq_hand_infos[0])
        collated_hand_infos = []
        for hand_idx in range(hand_nb):
            collated_hand_info = collate.collate(
                [hand[hand_idx] for hand in seq_hand_infos])
            collated_hand_info['label'] = collated_hand_info['label'][0]
            collated_hand_infos.append(collated_hand_info)

        obs = dict(hands=collated_hand_infos,
                   objects=[collate.collate(seq_obj_info)],
                   camera=collate.collate(seq_cameras),
                   setup=setup,
                   frame_idxs=frame_idxs,
                   images=images,
                   masks_hand=masks_hand,
                   masks_obj=masks_obj,
                   seq_idx=vid_info.seq_idx)

        return obs

    def get_hand_obj_info(self,
                          frame_info,
                          frame,
                          res=640,
                          roi=None,
                          affine_trans=None):
        hand_infos = []
        video_annots = self.annotations[frame_info.seq_idx]
        bbox_names = video_annots['bboxes_xyxy'].keys()
        bbox_infos = video_annots['bboxes_xyxy']
        setup = {"objects": 1}
        for bbox_name in bbox_names:
            setup[bbox_name] = 1
        has_left = "left_hand" in bbox_names
        has_right = "right_hand" in bbox_names

        if has_right:
            bbox = bbox_infos['right_hand'][frame] / self.resize_factor
            bbox = apply_bbox_transform(bbox, affine_trans)
            verts = np.random.random()
            verts = (np.random.rand(778, 3) * 0.2) + np.array([0, 0, 0.6])
            faces = self.faces["right"]
            hand_info = dict(
                verts3d=verts.astype(np.float32),
                faces=faces,
                label="right_hand",
                bbox=bbox.astype(np.float32),
            )
            hand_infos.append(hand_info)
        if has_left:
            bbox = bbox_infos['left_hand'][frame] / self.resize_factor
            bbox = apply_bbox_transform(bbox, affine_trans)
            verts = (np.random.rand(778, 3) * 0.2) + np.array([0, 0, 0.6])
            faces = self.faces["left"]
            hand_info = dict(
                verts3d=verts.astype(np.float32),
                faces=faces,
                label="left_hand",
                bbox=bbox.astype(np.float32),
            )
            hand_infos.append(hand_info)

        K = self.get_camintr()
        K = kcrop.get_K_crop_resize(
            torch.Tensor(K).unsqueeze(0), torch.Tensor([roi]),
            [res])[0].numpy()
        obj_info = self.object_models[frame_info.object]
        obj_bbox = bbox_infos["objects"][frame] / self.resize_factor
        obj_bbox = apply_bbox_transform(obj_bbox, affine_trans)
        verts3d = obj_info["verts"] + np.array([0, 0, 0.6])

        obj_info = dict(verts3d=verts3d.astype(np.float32),
                        faces=obj_info['faces'],
                        path=obj_info['path'],
                        canverts3d=obj_info["verts"].astype(np.float32),
                        bbox=obj_bbox)
        camera = dict(
            resolution=[res, res],  # WH
            K=K.astype(np.float32),
        )
        return obj_info, hand_infos, camera, setup

    def get_camintr(self):
        focal = 200
        cam_intr = np.array([
            [focal, 0, 640 // 2],
            [0, focal, 360 // 2],
            [0, 0, 1],
        ])
        return cam_intr

    def get_focal_nc(self):
        cam_intr = self.get_camintr()
        return (cam_intr[0, 0] + cam_intr[1, 1]) / 2 / max(self.image_size)

    def __len__(self):
        if self.mode == "vid":
            return len(self.vid_index)
        elif self.mode == "chunk":
            return len(self.chunk_index)
        else:
            raise ValueError(f"{self.mode} mode not in [frame|vid|chunk]")

    def project(self, points3d, cam_intr, camextr=None):
        if camextr is not None:
            points3d = np.array(self.camextr[:3, :3]).dot(
                points3d.transpose()).transpose()
        hom_2d = np.array(cam_intr).dot(points3d.transpose()).transpose()
        points2d = (hom_2d / hom_2d[:, 2:])[:, :2]
        return points2d.astype(np.float32)
