# Copyright 2021 University of New South Wales, University of Sydney, Ingham Institute

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import re
from pathlib import Path

import numpy as np

import torch

import SimpleITK as sitk

from imgaug import augmenters as iaa
from imgaug.augmentables.segmaps import SegmentationMapsOnImage

from loguru import logger

import math
import random
from scipy.ndimage import affine_transform
from scipy.ndimage.filters import gaussian_filter, median_filter

from platipy.imaging.cnn.utils import preprocess_image, resample_mask_to_image, get_contour_mask
from platipy.imaging.label.utils import get_union_mask, get_intersection_mask
from platipy.imaging.cnn.localise_net import LocaliseUNet
from platipy.imaging.utils.crop import label_to_roi, crop_to_roi


class GaussianNoise:
    def __init__(self, mu=0.0, sigma=0.0, probability=1.0):

        self.mu = mu
        self.sigma = sigma
        self.probability = probability

        if not hasattr(self.mu, "__iter__"):
            self.mu = (self.mu,) * 2

        if not hasattr(self.sigma, "__iter__"):
            self.sigma = (self.sigma,) * 2

    def apply(self, img, masks=[]):

        if random.random() > self.probability:
            # Don't augment this time
            return img, masks

        mean = random.uniform(self.mu[0], self.mu[1])
        sigma = random.uniform(self.sigma[0], self.sigma[1])

        gaussian = np.random.normal(mean, sigma, img.shape)
        return img + gaussian, masks


class GaussianBlur:
    def __init__(self, sigma=0.0, probability=1.0):

        self.sigma = sigma
        self.probability = probability

        if not hasattr(self.sigma, "__iter__"):
            self.sigma = (self.sigma,) * 2

    def apply(self, img, masks=[]):

        if random.random() > self.probability:
            # Don't augment this time
            return img, masks

        sigma = random.uniform(self.sigma[0], self.sigma[1])

        return gaussian_filter(img, sigma=sigma), masks


class MedianBlur:
    def __init__(self, size=1.0, probability=1.0):

        self.size = size
        self.probability = probability

        if not hasattr(self.size, "__iter__"):
            self.size = (self.size,) * 2

    def apply(self, img, masks=[]):

        if random.random() > self.probability:
            # Don't augment this time
            return img, masks

        size = random.randint(self.size[0], self.size[1])

        return median_filter(img, size=size), masks


DIMS = ["ax", "cor", "sag"]


class Affine:
    def __init__(
        self,
        scale={"ax": 1.0, "cor": 1.0, "sag": 1.0},
        translate_percent={"ax": 0.0, "cor": 0.0, "sag": 0.0},
        rotate={"ax": 0.0, "cor": 0.0, "sag": 0.0},
        shear={"ax": 0.0, "cor": 0.0, "sag": 0.0},
        mode="constant",
        cval=-1,
        probability=1.0,
    ):

        self.scale = scale
        self.translate_percent = translate_percent
        self.rotate = rotate
        self.shear = shear
        self.probability = probability

        for d in self.rotate:
            if not hasattr(self.rotate[d], "__iter__"):
                self.rotate[d] = (self.rotate[d],) * 2

        for d in self.scale:
            if not hasattr(self.scale[d], "__iter__"):
                self.scale[d] = (self.scale[d],) * 2

        for d in self.translate_percent:
            if not hasattr(self.translate_percent[d], "__iter__"):
                self.translate_percent[d] = (self.translate_percent[d],) * 2

        for d in self.shear:
            if not hasattr(self.shear[d], "__iter__"):
                self.shear[d] = (self.shear[d],) * 2

        for d in self.scale:
            if not hasattr(self.scale[d], "__iter__"):
                self.scale[d] = (self.scale[d],) * 2

    def get_rot(self, theta, d):
        if d == "ax":
            return np.matrix(
                [
                    [1, 0, 0, 0],
                    [0, math.cos(theta), -math.sin(theta), 0],
                    [0, math.sin(theta), math.cos(theta), 0],
                    [0, 0, 0, 1],
                ]
            )

        if d == "cor":
            return np.matrix(
                [
                    [math.cos(theta), 0, math.sin(theta), 0],
                    [0, 1, 0, 0],
                    [-math.sin(theta), 0, math.cos(theta), 0],
                    [0, 0, 0, 1],
                ]
            )

        if d == "sag":
            return np.matrix(
                [
                    [math.cos(theta), -math.sin(theta), 0, 0],
                    [math.sin(theta), math.cos(theta), 0, 0],
                    [0, 0, 1, 0],
                    [0, 0, 0, 1],
                ]
            )

    def get_shear(self, shear):

        mat = np.identity(4)
        mat[0, 1] = shear[1]
        mat[0, 2] = shear[2]
        mat[1, 0] = shear[0]
        mat[1, 2] = shear[2]
        mat[2, 0] = shear[0]
        mat[2, 1] = shear[1]

        return mat

    def apply(self, img, masks=[]):

        if random.random() > self.probability:
            # Don't augment this time
            return img, masks

        deg_to_rad = math.pi / 180

        t_prerot = np.identity(4)
        t_postrot = np.identity(4)
        for i, d in enumerate(DIMS):
            t_prerot[i, -1] = -img.shape[i] / 2
            t_postrot[i, -1] = img.shape[i] / 2

        t = t_postrot

        for i, d in enumerate(DIMS):
            t = t * self.get_rot(
                random.uniform(self.rotate[d][0], self.rotate[d][1]) * deg_to_rad, d
            )

        for i, d in enumerate(DIMS):
            scale = np.identity(4)
            scale[i, i] = 1 / random.uniform(self.scale[d][0], self.scale[d][1])
            t = t * scale

        shear = []
        for i, d in enumerate(DIMS):
            shear.append(random.uniform(self.shear[d][0], self.shear[d][1]))

        t = t * self.get_shear(shear)

        t = t * t_prerot

        for i, d in enumerate(DIMS):
            trans = [p * img.shape[i] for p in self.translate_percent[d]]
            translation = np.identity(4)
            translation[i, -1] = random.uniform(trans[0], trans[1])
            t = t * translation

        augmented_image = affine_transform(img, t, mode="mirror")
        augmented_masks = []
        for mask in masks:
            augmented_masks.append(affine_transform(mask, t, mode="nearest"))

        return augmented_image, augmented_masks


def crop_img_using_localise_model(
    img, localise_model, spacing=[1, 1, 1], crop_to_grid_size=[100, 100, 100]
):
    """Crops an image using a LocaliseUNet

    Args:
        img (SimpleITK.Image): The image to crop
        localise_model (str|Path|LocaliseUNet): The LocaliseUNet or path to checkpoint of
          LocaliseUNet.
        spacing (list, optional): The image spacing (mm) to resample to. Defaults to [1,1,1].
        crop_to_grid_size (list, optional): The size of the grid to crop to. Defaults to
          [100,100,100].

    Returns:
        SimpleITK.Image: The cropped image.
    """

    if isinstance(localise_model, str):
        localise_model = Path(localise_model)

    if isinstance(localise_model, Path):
        if localise_model.is_dir():
            # Find the first actual model checkpoint in this directory
            localise_model = next(localise_model.glob("*.ckpt"))

        localise_model = LocaliseUNet.load_from_checkpoint(localise_model)

    localise_model.eval()
    localise_pred = localise_model.infer(img)

    img = preprocess_image(img, spacing=spacing, crop_to_grid_size_xy=None)
    localise_pred = resample_mask_to_image(img, localise_pred)
    size, index = label_to_roi(localise_pred)

    if not hasattr(crop_to_grid_size, "__iter__"):
        crop_to_grid_size = (crop_to_grid_size,) * 3

    index = [i - int((g - s) / 2) for i, s, g in zip(index, size, crop_to_grid_size)]
    size = crop_to_grid_size
    img_size = img.GetSize()
    for i in range(3):
        if index[i] + size[i] >= img_size[i]:
            index[i] = img_size[i] - size[i] - 1
        if index[i] < 0:
            index[i] = 0

    return crop_to_roi(img, size, index)


def prepare_3d_transforms():
    affine_aug = Affine(
        translate_percent={"ax": [-0.1, 0.1], "cor": [-0.1, 0.1], "sag": [-0.1, 0.1]},
        rotate={"ax": [-10.0, 10.0], "cor": [-10.0, 10.0], "sag": [-10.0, 10.0]},
        scale={"ax": [0.8, 1.2], "cor": [0.8, 1.2], "sag": [0.8, 1.2]},
        shear={"ax": [0.0, 0.2], "cor": [0.0, 0.2], "sag": [0.0, 0.2]},
        probability=0.5,
    )
    gaussian_blur = GaussianBlur(sigma=[0.0, 1.0], probability=0.33)
    median_blur = MedianBlur(size=[1, 3], probability=0.5)
    gaussian_noise = GaussianNoise(sigma=[0, 0.2], probability=0.5)

    return [affine_aug, gaussian_blur, median_blur, gaussian_noise]


def prepare_transforms():

    sometimes = lambda aug: iaa.Sometimes(0.5, aug)

    seq = iaa.Sequential(
        [
            sometimes(
                iaa.Affine(
                    scale={"x": (0.8, 1.2), "y": (0.8, 1.2)},
                    translate_percent={"x": (-0.2, 0.2), "y": (-0.2, 0.2)},
                    rotate=(-15, 15),
                    shear=(-8, 8),
                    cval=-1,
                )
            ),
            # execute 0 to 2 of the following (less important) augmenters per image
            iaa.SomeOf(
                (0, 2),
                [
                    iaa.OneOf(
                        [
                            iaa.GaussianBlur((0, 1.5)),
                            iaa.AverageBlur(k=(3, 5)),
                        ]
                    ),
                    sometimes(iaa.PerspectiveTransform(scale=(0.01, 0.1))),
                ],
                random_order=True,
            ),
            sometimes(iaa.CoarseDropout((0.03, 0.15), size_percent=(0.02, 0.1))),
        ],
        random_order=True,
    )

    return seq


class NiftiDataset(torch.utils.data.Dataset):
    """PyTorch Dataset for processing Nifti data"""

    def __init__(
        self,
        data,
        working_dir,
        augment_on_fly=True,
        spacing=[1, 1, 1],
        crop_using_localise_model=None,
        crop_to_grid_size=128,
        contour_mask_kernel=5,
        combine_observers=None,
        intensity_scaling="window",
        intensity_window=[-500, 500],
        ndims=2,
    ):
        """Prepare a dataset from Nifti images/labels

        Args:
            data (list): List of dict's where each item contains keys: "image" and "label". Values
                are paths to the Nifti file. "label" may be a list where each item is a path to one
                observer.
            working_dir (str|path): Working directory where to write prepared files.
        """

        self.data = data
        self.transforms = None
        self.ndims = ndims
        if augment_on_fly:
            if self.ndims == 2:
                self.transforms = prepare_transforms()
            else:
                self.transforms = prepare_3d_transforms()
        self.slices = []
        self.working_dir = Path(working_dir)

        self.img_dir = working_dir.joinpath("img")
        self.label_dir = working_dir.joinpath("label")
        self.contour_mask_dir = working_dir.joinpath("contour_mask")

        self.img_dir.mkdir(exist_ok=True, parents=True)
        self.label_dir.mkdir(exist_ok=True, parents=True)
        self.contour_mask_dir.mkdir(exist_ok=True, parents=True)

        for case in data:
            case_id = case["id"]
            img_path = str(case["image"])

            structure_paths = case["label"]
            if isinstance(structure_paths, (str, Path)):
                structure_paths = [structure_paths]

            existing_images = [i for i in self.img_dir.glob(f"{case_id}_*.npy")]
            if len(existing_images) > 0:
                logger.debug(f"Image for case already exist: {case_id}")

                for img_path in existing_images:
                    z_matches = re.findall(fr"{case_id}_([0-9]*)\.npy", img_path.name)
                    if len(z_matches) == 0:
                        continue
                    z_slice = int(z_matches[0])

                    img_file = self.img_dir.joinpath(f"{case_id}_{z_slice}.npy")
                    assert img_file.exists()

                    contour_mask_file = self.contour_mask_dir.joinpath(f"{case_id}_{z_slice}.npy")
                    assert contour_mask_file.exists()

                    for obs in range(len(structure_paths)):
                        label_file = self.label_dir.joinpath(f"{case_id}_{obs}_{z_slice}.npy")
                        assert label_file.exists()
                        self.slices.append(
                            {
                                "z": z_slice,
                                "image": img_file,
                                "label": label_file,
                                "contour_mask": contour_mask_file,
                                "case": case_id,
                                "observer": obs,
                            }
                        )

                continue

            logger.debug(f"Generating images for case: {case_id}")
            img = sitk.ReadImage(img_path)

            if crop_using_localise_model:
                crop_img_using_localise_model(
                    img,
                    crop_using_localise_model,
                    spacing=spacing,
                    crop_to_grid_size=crop_to_grid_size,
                )
            else:
                img = preprocess_image(
                    img,
                    spacing=spacing,
                    crop_to_grid_size_xy=crop_to_grid_size,
                    intensity_scaling=intensity_scaling,
                    intensity_window=intensity_window,
                )

            observers = []
            for obs, structure_path in enumerate(structure_paths):
                structure_path = str(structure_path)
                label = sitk.ReadImage(structure_path)
                label = resample_mask_to_image(img, label)
                observers.append(label)

            contour_mask = get_contour_mask(observers, kernel=contour_mask_kernel)

            if combine_observers == "union":
                observers = [get_union_mask(observers)]

            if combine_observers == "intersection":
                observers = [get_intersection_mask(observers)]

            z_range = range(img.GetSize()[2])
            if ndims == 3:
                z_range = range(1)
            for z_slice in z_range:

                # Save the image slice
                if ndims == 2:
                    img_slice = img[:, :, z_slice]
                else:
                    img_slice = img

                img_file = self.img_dir.joinpath(f"{case_id}_{z_slice}.npy")
                np.save(img_file, sitk.GetArrayFromImage(img_slice))

                # Save the contour mask slice
                if ndims == 2:
                    contour_mask_slice = contour_mask[:, :, z_slice]
                else:
                    contour_mask_slice = contour_mask
                contour_mask_file = self.contour_mask_dir.joinpath(f"{case_id}_{z_slice}.npy")
                np.save(contour_mask_file, sitk.GetArrayFromImage(contour_mask_slice))

                for obs, label in enumerate(observers):

                    if ndims == 2:
                        label_slice = label[:, :, z_slice]
                    else:
                        label_slice = label
                    label_file = self.label_dir.joinpath(f"{case_id}_{obs}_{z_slice}.npy")
                    np.save(label_file, sitk.GetArrayFromImage(label_slice).astype(np.int8))
                    self.slices.append(
                        {
                            "z": z_slice,
                            "image": img_file,
                            "label": label_file,
                            "contour_mask": contour_mask_file,
                            "case": case_id,
                            "observer": obs,
                        }
                    )

    def __len__(self):
        return len(self.slices)

    def __getitem__(self, index):

        img = np.load(self.slices[index]["image"])
        label = np.load(self.slices[index]["label"])
        contour_mask = np.load(self.slices[index]["contour_mask"])

        if self.transforms:
            if self.ndims == 2:
                seg_arr = np.concatenate(
                    (np.expand_dims(label, 2), np.expand_dims(contour_mask, 2)), 2
                )
                segmap = SegmentationMapsOnImage(seg_arr, shape=label.shape)
                img, seg = self.transforms(image=img, segmentation_maps=segmap)
                label = seg.get_arr()[:, :, 0].squeeze()
                contour_mask = seg.get_arr()[:, :, 1].squeeze()
            else:
                masks = [label, contour_mask]
                for aug in self.transforms:
                    img, masks = aug.apply(img, masks)
                label = masks[0]
                contour_mask = masks[1]

        img = torch.FloatTensor(img)
        label = torch.IntTensor(label)
        contour_mask = torch.FloatTensor(contour_mask)

        return (
            img.unsqueeze(0),
            label,
            contour_mask,
            {
                "case": self.slices[index]["case"],
                "observer": self.slices[index]["observer"],
                "z": self.slices[index]["z"],
            },
        )