from transformers import CLIPSegProcessor, CLIPSegForImageSegmentation

from PIL import Image
import torch
import torchvision.transforms as T
import numpy as np

from torchvision.transforms.functional import to_pil_image
import matplotlib.pyplot as plt
import matplotlib.cm as cm

import cv2

from scipy.ndimage import gaussian_filter

from typing import Optional, Tuple

import warnings
warnings.filterwarnings("ignore", category=UserWarning, module="torch")
warnings.filterwarnings("ignore", category=UserWarning, module="safetensors")

import comfy.utils


"""Helper methods for CLIPSeg nodes"""

def tensor_to_numpy(tensor: torch.Tensor) -> np.ndarray:
    """Convert a tensor to a numpy array and scale its values to 0-255."""
    array = tensor.numpy().squeeze()
    return (array * 255).astype(np.uint8)

def numpy_to_tensor(array: np.ndarray, dtype) -> torch.Tensor:
    """Convert a numpy array to a tensor and scale its values from 0-255 to 0-1."""
    array = array.astype(np.float32) / 255.0
    return torch.from_numpy(array).type(dtype)[None,]

def apply_colormap(mask: torch.Tensor, colormap) -> np.ndarray:
    """Apply a colormap to a tensor and convert it to a numpy array."""
    colored_mask = colormap(mask.numpy())[:, :, :3]
    return (colored_mask * 255).astype(np.uint8)

def resize_image(image: np.ndarray, dimensions: Tuple[int, int]) -> np.ndarray:
    """Resize an image to the given dimensions using linear interpolation."""
    return cv2.resize(image, dimensions, interpolation=cv2.INTER_LINEAR)

def overlay_image(background: np.ndarray, foreground: np.ndarray, alpha: float) -> np.ndarray:
    """Overlay the foreground image onto the background with a given opacity (alpha)."""
    return cv2.addWeighted(background, 1 - alpha, foreground, alpha, 0)

def dilate_mask(mask: torch.Tensor, dilation_factor: float, dtype) -> torch.Tensor:
    """Dilate a mask using a square kernel with a given dilation factor."""
    kernel_size = int(dilation_factor * 2) + 1
    kernel = np.ones((kernel_size, kernel_size), np.uint8)
    mask_dilated = cv2.dilate(mask.numpy(), kernel, iterations=1)
    return torch.from_numpy(mask_dilated).type(dtype)

def get_heatmap(mask_dilated, image, dtype):
    # Convert the mask to a heatmap
    heatmap = apply_colormap(mask_dilated, cm.viridis)

    # Overlay the heatmap on the original image
    dimensions = (image.shape[1], image.shape[0])
    heatmap_resized = resize_image(heatmap, dimensions)

    overlay_heatmap = overlay_image(image, heatmap_resized, 0.5)

    # Convert the numpy arrays to tensors
    return numpy_to_tensor(overlay_heatmap, dtype)

def get_binary(mask_dilated, image, dtype):
    # Convert the mask to a binary mask
    binary_mask = apply_colormap(mask_dilated, cm.Greys_r)

    # Overlay the binary mask on the original image
    dimensions = (image.shape[1], image.shape[0])
    binary_mask_resized = resize_image(binary_mask, dimensions)

    overlay_binary = overlay_image(image, binary_mask_resized, 1)

    # Convert the numpy arrays to tensors
    return numpy_to_tensor(overlay_binary, dtype), binary_mask_resized

class CLIPSeg:

    def __init__(self):
        self.dtype = torch.float16 if comfy.model_management.should_use_fp16() else torch.float32
        self.device = comfy.model_management.get_torch_device()

    @classmethod
    def INPUT_TYPES(s):
        """
            Return a dictionary which contains config for all input fields.
            Some types (string): "MODEL", "VAE", "CLIP", "CONDITIONING", "LATENT", "IMAGE", "INT", "STRING", "FLOAT".
            Input types "INT", "STRING" or "FLOAT" are special values for fields on the node.
            The type can be a list for selection.

            Returns: `dict`:
                - Key input_fields_group (`string`): Can be either required, hidden or optional. A node class must have property `required`
                - Value input_fields (`dict`): Contains input fields config:
                    * Key field_name (`string`): Name of a entry-point method's argument
                    * Value field_config (`tuple`):
                        + First value is a string indicate the type of field or a list for selection.
                        + Secound value is a config for type "INT", "STRING" or "FLOAT".
        """
        return {"required":
                    {
                        "image": ("IMAGE",),
                        "prompt": ("STRING", {"multiline": False}),
                     },
                "optional":
                    {
                        "blur": ("FLOAT", {"min": 0, "max": 15, "step": 0.1, "default": 7}),
                        "threshold": ("FLOAT", {"min": 0, "max": 1, "step": 0.05, "default": 0.4}),
                        "dilation_factor": ("INT", {"min": 0, "max": 10, "step": 1, "default": 4}),
                    }
                }

    CATEGORY = "image"
    RETURN_TYPES = ("MASK", "IMAGE", "IMAGE",)
    RETURN_NAMES = ("Mask","Heatmap Mask", "BW Mask")

    FUNCTION = "segment_image"
    def segment_image(self, image: torch.Tensor, prompt: str, blur: float, threshold: float, dilation_factor: int) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """Create a segmentation mask from an image and a text prompt using CLIPSeg.

        Args:
            image (torch.Tensor): The image to segment.
            prompt (str): The text prompt to use for segmentation.
            blur (float): How much to blur the segmentation mask.
            threshold (float): The threshold to use for binarizing the segmentation mask.
            dilation_factor (int): How much to dilate the segmentation mask.

        Returns:
            Tuple[torch.Tensor, torch.Tensor, torch.Tensor]: The segmentation mask, the heatmap mask, and the binarized mask.
        """

        pil_image = to_pil_image(image, mode="RGB")
        flat = self.do_clipseg(prompt, [pil_image])
        mask_dilated = self.get_mask_dilated(flat, threshold, blur, dilation_factor)

        image_out_heatmap = get_heatmap(mask_dilated, pil_image)
        image_out_binary, binary_mask_resized = get_binary(mask_dilated, pil_image, self.dtype)

        # Save or display the resulting binary mask
        binary_mask_image = Image.fromarray(binary_mask_resized[..., 0])

        # convert PIL image to numpy array
        tensor_bw = binary_mask_image.convert("RGB")
        tensor_bw = np.array(tensor_bw).astype(np.float32) / 255.0
        tensor_bw = torch.from_numpy(tensor_bw).type(self.dtype)[None,]
        tensor_bw = tensor_bw.squeeze(0)[..., 0]

        return tensor_bw, image_out_heatmap, image_out_binary

    def do_clipseg(self, prompt, images):
        # see https://huggingface.co/blog/clipseg-zero-shot
        processor = CLIPSegProcessor.from_pretrained("./clipseg-rd64-refined")
        model = CLIPSegForImageSegmentation.from_pretrained("./clipseg-rd64-refined").to(self.device)

        inputs = processor(text=prompt, images=images, return_tensors="pt")

        # Predict the segemntation mask
        with torch.no_grad():
            outputs = model(**inputs)

        preds = outputs.logits
        return preds[0]
    
    def get_mask_dilated(self, flat, threshold, blur, dilation_factor):
        # get the mask
        tensor = torch.sigmoid(flat)

        # Apply a threshold to the original tensor to cut off low values
        tensor_thresholded = torch.where(tensor > threshold, tensor, torch.tensor(0, dtype=self.dtype))

        # Apply Gaussian blur to the thresholded tensor
        tensor_smoothed = torch.from_numpy(
            gaussian_filter(tensor_thresholded.numpy(), sigma=blur)
        ).type(self.dtype)

        # Normalize the smoothed tensor to [0, 1]
        mask_normalized = (tensor_smoothed - tensor_smoothed.min()) / (tensor_smoothed.max() - tensor_smoothed.min())

        # Dilate the normalized mask
        return dilate_mask(mask_normalized, dilation_factor)

class CombineMasks:
    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(s):
        return {"required":
                    {
                        "input_image": ("IMAGE", ),
                        "mask_1": ("MASK", ), 
                        "mask_2": ("MASK", ),
                    },
                "optional": 
                    {
                        "mask_3": ("MASK",), 
                    },
                }

    CATEGORY = "image"
    RETURN_TYPES = ("MASK", "IMAGE", "IMAGE",)
    RETURN_NAMES = ("Combined Mask","Heatmap Mask", "BW Mask")

    FUNCTION = "combine_masks"

    def combine_masks(self, input_image: torch.Tensor, mask_1: torch.Tensor, mask_2: torch.Tensor, mask_3: Optional[torch.Tensor] = None) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """A method that combines two or three masks into one mask. Takes in tensors and returns the mask as a tensor, as well as the heatmap and binary mask as tensors."""

        # Combine masks
        combined_mask = mask_1 + mask_2 + mask_3 if mask_3 is not None else mask_1 + mask_2


        # Convert image and masks to numpy arrays
        image_np = tensor_to_numpy(input_image)
        heatmap = apply_colormap(combined_mask, cm.viridis)
        binary_mask = apply_colormap(combined_mask, cm.Greys_r)

        # Resize heatmap and binary mask to match the original image dimensions
        dimensions = (image_np.shape[1], image_np.shape[0])
        heatmap_resized = resize_image(heatmap, dimensions)
        binary_mask_resized = resize_image(binary_mask, dimensions)

        # Overlay the heatmap and binary mask onto the original image
        alpha_heatmap, alpha_binary = 0.5, 1
        overlay_heatmap = overlay_image(image_np, heatmap_resized, alpha_heatmap)
        overlay_binary = overlay_image(image_np, binary_mask_resized, alpha_binary)

        # Convert overlays to tensors
        image_out_heatmap = numpy_to_tensor(overlay_heatmap)
        image_out_binary = numpy_to_tensor(overlay_binary)

        return combined_mask, image_out_heatmap, image_out_binary

# A dictionary that contains all nodes you want to export with their names
# NOTE: names should be globally unique
NODE_CLASS_MAPPINGS = {
    "CLIPSeg": CLIPSeg,
    "CombineSegMasks": CombineMasks,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "CLIPSeg": "CLIPSeg",
    "CombineSegMasks": "Combine Seg Masks",
}