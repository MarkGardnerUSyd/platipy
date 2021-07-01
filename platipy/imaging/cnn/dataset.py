from pathlib import Path

import torch

import SimpleITK as sitk

from imgaug import augmenters as iaa
from imgaug.augmentables.segmaps import SegmentationMapsOnImage

from loguru import logger


def preprocess_image(img, crop_to_mm=128):

    img = sitk.Normalize(img)

    new_spacing = sitk.VectorDouble(3)
    new_spacing[0] = 1.0
    new_spacing[1] = 1.0
    new_spacing[2] = img.GetSpacing()[2]

    new_size = sitk.VectorUInt32(3)
    new_size[0] = int(img.GetSize()[0] * img.GetSpacing()[0])
    new_size[1] = int(img.GetSize()[1] * img.GetSpacing()[1])
    new_size[2] = int(img.GetSize()[2])

    if new_size[0] < crop_to_mm:
        new_size[0] = crop_to_mm

    if new_size[1] < crop_to_mm:
        new_size[1] = crop_to_mm

    img = sitk.Resample(
        img,
        new_size,
        sitk.Transform(),
        sitk.sitkLinear,
        img.GetOrigin(),
        new_spacing,
        img.GetDirection(),
        -1,
        img.GetPixelID(),
    )

    center_x = img.GetSize()[0] / 2
    x_from = int(center_x - crop_to_mm / 2)
    x_to = x_from + crop_to_mm

    center_y = img.GetSize()[1] / 2
    y_from = int(center_y - crop_to_mm / 2)
    y_to = y_from + crop_to_mm

    img = img[x_from:x_to, y_from:y_to, :]

    return img


def resample_mask_to_image(img, mask):

    return sitk.Resample(
        mask,
        img,
        sitk.Transform(),
        sitk.sitkNearestNeighbor,
        0,
        mask.GetPixelID(),
    )


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
            # don't execute all of them, as that would often be way too strong
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
        ],
        random_order=True,
    )

    return seq


class NiftiDataset(torch.utils.data.Dataset):
    """PyTorch Dataset for processing Nifti data"""

    def __init__(self, data, working_dir):
        """Prepare a dataset from Nifti images/labels

        Args:
            data (list): List of dict's where each item contains keys: "image" and "label". Values
                are paths to the Nifti file. "label" may be a list where each item is a path to one
                observer.
            working_dir (str|path): Working directory where to write prepared files.
        """

        self.data = data
        self.transforms = prepare_transforms()
        self.slices = []
        self.working_dir = Path(working_dir)

        self.img_dir = working_dir.joinpath("img")
        self.mask_dir = working_dir.joinpath("mask")

        self.img_dir.mkdir(exist_ok=True, parents=True)
        self.mask_dir.mkdir(exist_ok=True, parents=True)

        for case in data:
            case_id = case["id"]
            img_path = str(case["image"])

            structure_paths = case["label"]
            if isinstance(structure_paths, (str, Path)):
                structure_paths = [structure_paths]

            img_file = self.img_dir.joinpath(f"{case_id}.nii.gz")
            img = sitk.ReadImage(img_path)

            if not img_file.exists():
                img = preprocess_image(img)
                sitk.WriteImage(img, str(img_file))

                for obs, structure_path in enumerate(structure_paths):
                    structure_path = str(structure_path)
                    mask = sitk.ReadImage(structure_path)
                    mask = resample_mask_to_image(img, mask)
                    mask_file = self.mask_dir.joinpath(f"{case_id}_{obs}.nii.gz")
                    sitk.WriteImage(mask, str(mask_file))

            for z_slice in range(img.GetSize()[2]):
                for obs, mask in enumerate(structure_paths):
                    mask_file = self.mask_dir.joinpath(f"{case_id}_{obs}.nii.gz")
                    self.slices.append({"z": z_slice, "image": img_file, "mask": mask_file})

    def __len__(self):
        return len(self.slices)

    def __getitem__(self, index):

        img_file = self.slices[index]["image"]
        mask_file = self.slices[index]["mask"]
        z_slice = self.slices[index]["z"]
        
        img = sitk.ReadImage(str(img_file))
        img = sitk.GetArrayFromImage(img)
        img = img[z_slice, :, :]

        mask = sitk.ReadImage(str(mask_file))
        mask = sitk.GetArrayFromImage(mask)
        mask = mask[z_slice, :, :]

        segmap = SegmentationMapsOnImage(mask, shape=mask.shape)
        img, mask = self.transforms(image=img, segmentation_maps=segmap)
        mask = mask.get_arr()

        img = torch.FloatTensor(img)
        mask = torch.LongTensor(mask)

        return img.unsqueeze(0), mask