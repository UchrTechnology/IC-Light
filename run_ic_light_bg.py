import os
import math
import numpy as np
import torch
import safetensors.torch as sf
import db_examples

from PIL import Image
from diffusers import StableDiffusionPipeline, StableDiffusionImg2ImgPipeline
from diffusers import AutoencoderKL, UNet2DConditionModel, DDIMScheduler, EulerAncestralDiscreteScheduler, DPMSolverMultistepScheduler
from diffusers.models.attention_processor import AttnProcessor2_0
from transformers import CLIPTextModel, CLIPTokenizer
from briarmbg import BriaRMBG
from enum import Enum
from torch.hub import download_url_to_file


import argparse


if os.path.isdir("/app/iei-seisaku-pipe-v3"):
    # use downloaded weights
    local_sd15_path = '/app/iei-seisaku-pipe-v3/IC-Light/models/realistic-vision-v51'
    local_rmbg_path = '/app/iei-seisaku-pipe-v3/IC-Light/models/briaai-RMBG-1.4'
    tokenizer = CLIPTokenizer.from_pretrained(f"{local_sd15_path}/tokenizer")
    text_encoder = CLIPTextModel.from_pretrained(f"{local_sd15_path}/text_encoder")
    vae = AutoencoderKL.from_pretrained(f"{local_sd15_path}/vae")
    unet = UNet2DConditionModel.from_pretrained(f"{local_sd15_path}/unet")
    rmbg = BriaRMBG.from_pretrained(local_rmbg_path)
else:
    # 'stablediffusionapi/realistic-vision-v51'
    # 'runwayml/stable-diffusion-v1-5'
    sd15_name = 'stablediffusionapi/realistic-vision-v51'
    tokenizer = CLIPTokenizer.from_pretrained(sd15_name, subfolder="tokenizer")
    text_encoder = CLIPTextModel.from_pretrained(sd15_name, subfolder="text_encoder")
    vae = AutoencoderKL.from_pretrained(sd15_name, subfolder="vae")
    unet = UNet2DConditionModel.from_pretrained(sd15_name, subfolder="unet")
    rmbg = BriaRMBG.from_pretrained("briaai/RMBG-1.4")

# Change UNet

with torch.no_grad():
    new_conv_in = torch.nn.Conv2d(12, unet.conv_in.out_channels, unet.conv_in.kernel_size, unet.conv_in.stride, unet.conv_in.padding)
    new_conv_in.weight.zero_()
    new_conv_in.weight[:, :4, :, :].copy_(unet.conv_in.weight)
    new_conv_in.bias = unet.conv_in.bias
    unet.conv_in = new_conv_in

unet_original_forward = unet.forward


def hooked_unet_forward(sample, timestep, encoder_hidden_states, **kwargs):
    c_concat = kwargs['cross_attention_kwargs']['concat_conds'].to(sample)
    c_concat = torch.cat([c_concat] * (sample.shape[0] // c_concat.shape[0]), dim=0)
    new_sample = torch.cat([sample, c_concat], dim=1)
    kwargs['cross_attention_kwargs'] = {}
    return unet_original_forward(new_sample, timestep, encoder_hidden_states, **kwargs)


unet.forward = hooked_unet_forward

# Load

model_path = '/app/iei-seisaku-pipe-v3/IC-Light/models/iclight_sd15_fbc.safetensors'
if not os.path.exists(model_path):
    model_path = './models/iclight_sd15_fbc.safetensors'
if not os.path.exists(model_path):
    print("download iclight model")
    download_url_to_file(url='https://huggingface.co/lllyasviel/ic-light/resolve/main/iclight_sd15_fbc.safetensors', dst=model_path)

sd_offset = sf.load_file(model_path)
sd_origin = unet.state_dict()
keys = sd_origin.keys()
sd_merged = {k: sd_origin[k] + sd_offset[k] for k in sd_origin.keys()}
unet.load_state_dict(sd_merged, strict=True)
del sd_offset, sd_origin, sd_merged, keys

# Device

device = torch.device('cuda')
text_encoder = text_encoder.to(device=device, dtype=torch.float16)
vae = vae.to(device=device, dtype=torch.bfloat16)
unet = unet.to(device=device, dtype=torch.float16)
rmbg = rmbg.to(device=device, dtype=torch.float32)

# SDP

unet.set_attn_processor(AttnProcessor2_0())
vae.set_attn_processor(AttnProcessor2_0())

# Samplers

ddim_scheduler = DDIMScheduler(
    num_train_timesteps=1000,
    beta_start=0.00085,
    beta_end=0.012,
    beta_schedule="scaled_linear",
    clip_sample=False,
    set_alpha_to_one=False,
    steps_offset=1,
)

euler_a_scheduler = EulerAncestralDiscreteScheduler(
    num_train_timesteps=1000,
    beta_start=0.00085,
    beta_end=0.012,
    steps_offset=1
)

dpmpp_2m_sde_karras_scheduler = DPMSolverMultistepScheduler(
    num_train_timesteps=1000,
    beta_start=0.00085,
    beta_end=0.012,
    algorithm_type="sde-dpmsolver++",
    use_karras_sigmas=True,
    steps_offset=1
)

# Pipelines

t2i_pipe = StableDiffusionPipeline(
    vae=vae,
    text_encoder=text_encoder,
    tokenizer=tokenizer,
    unet=unet,
    scheduler=dpmpp_2m_sde_karras_scheduler,
    safety_checker=None,
    requires_safety_checker=False,
    feature_extractor=None,
    image_encoder=None
)

i2i_pipe = StableDiffusionImg2ImgPipeline(
    vae=vae,
    text_encoder=text_encoder,
    tokenizer=tokenizer,
    unet=unet,
    scheduler=dpmpp_2m_sde_karras_scheduler,
    safety_checker=None,
    requires_safety_checker=False,
    feature_extractor=None,
    image_encoder=None
)


@torch.inference_mode()
def encode_prompt_inner(txt: str):
    max_length = tokenizer.model_max_length
    chunk_length = tokenizer.model_max_length - 2
    id_start = tokenizer.bos_token_id
    id_end = tokenizer.eos_token_id
    id_pad = id_end

    def pad(x, p, i):
        return x[:i] if len(x) >= i else x + [p] * (i - len(x))

    tokens = tokenizer(txt, truncation=False, add_special_tokens=False)["input_ids"]
    chunks = [[id_start] + tokens[i: i + chunk_length] + [id_end] for i in range(0, len(tokens), chunk_length)]
    chunks = [pad(ck, id_pad, max_length) for ck in chunks]

    token_ids = torch.tensor(chunks).to(device=device, dtype=torch.int64)
    conds = text_encoder(token_ids).last_hidden_state

    return conds


@torch.inference_mode()
def encode_prompt_pair(positive_prompt, negative_prompt):
    c = encode_prompt_inner(positive_prompt)
    uc = encode_prompt_inner(negative_prompt)

    c_len = float(len(c))
    uc_len = float(len(uc))
    max_count = max(c_len, uc_len)
    c_repeat = int(math.ceil(max_count / c_len))
    uc_repeat = int(math.ceil(max_count / uc_len))
    max_chunk = max(len(c), len(uc))

    c = torch.cat([c] * c_repeat, dim=0)[:max_chunk]
    uc = torch.cat([uc] * uc_repeat, dim=0)[:max_chunk]

    c = torch.cat([p[None, ...] for p in c], dim=1)
    uc = torch.cat([p[None, ...] for p in uc], dim=1)

    return c, uc


@torch.inference_mode()
def pytorch2numpy(imgs, quant=True):
    results = []
    for x in imgs:
        y = x.movedim(0, -1)

        if quant:
            y = y * 127.5 + 127.5
            y = y.detach().float().cpu().numpy().clip(0, 255).astype(np.uint8)
        else:
            y = y * 0.5 + 0.5
            y = y.detach().float().cpu().numpy().clip(0, 1).astype(np.float32)

        results.append(y)
    return results


@torch.inference_mode()
def numpy2pytorch(imgs):
    h = torch.from_numpy(np.stack(imgs, axis=0)).float() / 127.0 - 1.0  # so that 127 must be strictly 0.0
    h = h.movedim(-1, 1)
    return h


def resize_and_center_crop(image, target_width, target_height):
    pil_image = Image.fromarray(image)
    original_width, original_height = pil_image.size
    scale_factor = max(target_width / original_width, target_height / original_height)
    resized_width = int(round(original_width * scale_factor))
    resized_height = int(round(original_height * scale_factor))
    resized_image = pil_image.resize((resized_width, resized_height), Image.LANCZOS)
    left = (resized_width - target_width) / 2
    top = (resized_height - target_height) / 2
    right = (resized_width + target_width) / 2
    bottom = (resized_height + target_height) / 2
    cropped_image = resized_image.crop((left, top, right, bottom))
    return np.array(cropped_image)


def resize_without_crop(image, target_width, target_height):
    pil_image = Image.fromarray(image)
    resized_image = pil_image.resize((target_width, target_height), Image.LANCZOS)
    return np.array(resized_image)


@torch.inference_mode()
def run_rmbg(img, sigma=0.0):
    H, W, C = img.shape
    assert C == 3
    k = (256.0 / float(H * W)) ** 0.5
    feed = resize_without_crop(img, int(64 * round(W * k)), int(64 * round(H * k)))
    feed = numpy2pytorch([feed]).to(device=device, dtype=torch.float32)
    alpha = rmbg(feed)[0][0]
    alpha = torch.nn.functional.interpolate(alpha, size=(H, W), mode="bilinear")
    alpha = alpha.movedim(1, -1)[0]
    alpha = alpha.detach().float().cpu().numpy().clip(0, 1)
    result = 127 + (img.astype(np.float32) - 127 + sigma) * alpha
    return result.clip(0, 255).astype(np.uint8), alpha


@torch.inference_mode()
def process(input_fg, input_bg, prompt, image_width, image_height, num_samples, seed, steps, a_prompt, n_prompt, cfg, highres_scale, highres_denoise, bg_source):
    bg_source = BGSource(bg_source)

    if bg_source == BGSource.UPLOAD:
        pass
    elif bg_source == BGSource.UPLOAD_FLIP:
        input_bg = np.fliplr(input_bg)
    elif bg_source == BGSource.GREY:
        input_bg = np.zeros(shape=(image_height, image_width, 3), dtype=np.uint8) + 64
    elif bg_source == BGSource.LEFT:
        gradient = np.linspace(224, 32, image_width)
        image = np.tile(gradient, (image_height, 1))
        input_bg = np.stack((image,) * 3, axis=-1).astype(np.uint8)
    elif bg_source == BGSource.RIGHT:
        gradient = np.linspace(32, 224, image_width)
        image = np.tile(gradient, (image_height, 1))
        input_bg = np.stack((image,) * 3, axis=-1).astype(np.uint8)
    elif bg_source == BGSource.TOP:
        gradient = np.linspace(224, 32, image_height)[:, None]
        image = np.tile(gradient, (1, image_width))
        input_bg = np.stack((image,) * 3, axis=-1).astype(np.uint8)
    elif bg_source == BGSource.BOTTOM:
        gradient = np.linspace(32, 224, image_height)[:, None]
        image = np.tile(gradient, (1, image_width))
        input_bg = np.stack((image,) * 3, axis=-1).astype(np.uint8)
    elif bg_source == BGSource.CUSTOM_LEFT:
        gradient = np.linspace(192, 64, image_width)
        image_1 = np.tile(gradient, (image_height, 1))
        gradient = np.linspace(192, 64, image_height)[:, None]
        image_2 = np.tile(gradient, (1, image_width))
        image = (image_1 + image_2) / 2
        input_bg = np.stack((image,) * 3, axis=-1).astype(np.uint8)
    elif bg_source == BGSource.CUSTOM_LEFT_HIGH:
        gradient = np.linspace(224, 32, image_width)
        image_1 = np.tile(gradient, (image_height, 1))
        gradient = np.linspace(224, 32, image_height)[:, None]
        image_2 = np.tile(gradient, (1, image_width))
        image = (image_1 + image_2) / 2
        input_bg = np.stack((image,) * 3, axis=-1).astype(np.uint8)
    elif bg_source == BGSource.CUSTOM_RIGHT:
        gradient = np.linspace(64, 192, image_width)
        image_1 = np.tile(gradient, (image_height, 1))
        gradient = np.linspace(192, 64, image_height)[:, None]
        image_2 = np.tile(gradient, (1, image_width))
        image = (image_1 + image_2) / 2
        input_bg = np.stack((image,) * 3, axis=-1).astype(np.uint8)
    elif bg_source == BGSource.CUSTOM_RIGHT_HIGH:
        gradient = np.linspace(32, 224, image_width)
        image_1 = np.tile(gradient, (image_height, 1))
        gradient = np.linspace(224, 32, image_height)[:, None]
        image_2 = np.tile(gradient, (1, image_width))
        image = (image_1 + image_2) / 2
        input_bg = np.stack((image,) * 3, axis=-1).astype(np.uint8)
    elif bg_source == BGSource.CUSTOM_GRAY:
        input_bg = np.zeros(shape=(image_height, image_width, 3), dtype=np.uint8) + 100
    else:
        raise 'Wrong background source!'

    rng = torch.Generator(device=device).manual_seed(seed)

    fg = resize_and_center_crop(input_fg, image_width, image_height)
    bg = resize_and_center_crop(input_bg, image_width, image_height)
    concat_conds = numpy2pytorch([fg, bg]).to(device=vae.device, dtype=vae.dtype)
    concat_conds = vae.encode(concat_conds).latent_dist.mode() * vae.config.scaling_factor
    concat_conds = torch.cat([c[None, ...] for c in concat_conds], dim=1)

    conds, unconds = encode_prompt_pair(positive_prompt=prompt + ', ' + a_prompt, negative_prompt=n_prompt)

    latents = t2i_pipe(
        prompt_embeds=conds,
        negative_prompt_embeds=unconds,
        width=image_width,
        height=image_height,
        num_inference_steps=steps,
        num_images_per_prompt=num_samples,
        generator=rng,
        output_type='latent',
        guidance_scale=cfg,
        cross_attention_kwargs={'concat_conds': concat_conds},
    ).images.to(vae.dtype) / vae.config.scaling_factor

    pixels = vae.decode(latents).sample
    pixels = pytorch2numpy(pixels)
    pixels = [resize_without_crop(
        image=p,
        target_width=int(round(image_width * highres_scale / 64.0) * 64),
        target_height=int(round(image_height * highres_scale / 64.0) * 64))
    for p in pixels]

    pixels = numpy2pytorch(pixels).to(device=vae.device, dtype=vae.dtype)
    latents = vae.encode(pixels).latent_dist.mode() * vae.config.scaling_factor
    latents = latents.to(device=unet.device, dtype=unet.dtype)

    image_height, image_width = latents.shape[2] * 8, latents.shape[3] * 8
    fg = resize_and_center_crop(input_fg, image_width, image_height)
    bg = resize_and_center_crop(input_bg, image_width, image_height)
    concat_conds = numpy2pytorch([fg, bg]).to(device=vae.device, dtype=vae.dtype)
    concat_conds = vae.encode(concat_conds).latent_dist.mode() * vae.config.scaling_factor
    concat_conds = torch.cat([c[None, ...] for c in concat_conds], dim=1)

    latents = i2i_pipe(
        image=latents,
        strength=highres_denoise,
        prompt_embeds=conds,
        negative_prompt_embeds=unconds,
        width=image_width,
        height=image_height,
        num_inference_steps=int(round(steps / highres_denoise)),
        num_images_per_prompt=num_samples,
        generator=rng,
        output_type='latent',
        guidance_scale=cfg,
        cross_attention_kwargs={'concat_conds': concat_conds},
    ).images.to(vae.dtype) / vae.config.scaling_factor

    pixels = vae.decode(latents).sample
    pixels = pytorch2numpy(pixels, quant=False)

    return pixels, [fg, bg]


@torch.inference_mode()
def process_relight(input_fg, input_bg, prompt, image_width, image_height, num_samples, seed, steps, a_prompt, n_prompt, cfg, highres_scale, highres_denoise, bg_source):
    input_fg, matting = run_rmbg(input_fg)
    results, extra_images = process(input_fg, input_bg, prompt, image_width, image_height, num_samples, seed, steps, a_prompt, n_prompt, cfg, highres_scale, highres_denoise, bg_source)
    results = [(x * 255.0).clip(0, 255).astype(np.uint8) for x in results]
    return results + extra_images


@torch.inference_mode()
def process_normal(input_fg, input_bg, prompt, image_width, image_height, num_samples, seed, steps, a_prompt, n_prompt, cfg, highres_scale, highres_denoise, bg_source):
    input_fg, matting = run_rmbg(input_fg, sigma=16)

    print('left ...')
    left = process(input_fg, input_bg, prompt, image_width, image_height, 1, seed, steps, a_prompt, n_prompt, cfg, highres_scale, highres_denoise, BGSource.LEFT.value)[0][0]

    print('right ...')
    right = process(input_fg, input_bg, prompt, image_width, image_height, 1, seed, steps, a_prompt, n_prompt, cfg, highres_scale, highres_denoise, BGSource.RIGHT.value)[0][0]

    print('bottom ...')
    bottom = process(input_fg, input_bg, prompt, image_width, image_height, 1, seed, steps, a_prompt, n_prompt, cfg, highres_scale, highres_denoise, BGSource.BOTTOM.value)[0][0]

    print('top ...')
    top = process(input_fg, input_bg, prompt, image_width, image_height, 1, seed, steps, a_prompt, n_prompt, cfg, highres_scale, highres_denoise, BGSource.TOP.value)[0][0]

    inner_results = [left * 2.0 - 1.0, right * 2.0 - 1.0, bottom * 2.0 - 1.0, top * 2.0 - 1.0]

    ambient = (left + right + bottom + top) / 4.0
    h, w, _ = ambient.shape
    matting = resize_and_center_crop((matting[..., 0] * 255.0).clip(0, 255).astype(np.uint8), w, h).astype(np.float32)[..., None] / 255.0

    def safa_divide(a, b):
        e = 1e-5
        return ((a + e) / (b + e)) - 1.0

    left = safa_divide(left, ambient)
    right = safa_divide(right, ambient)
    bottom = safa_divide(bottom, ambient)
    top = safa_divide(top, ambient)

    u = (right - left) * 0.5
    v = (top - bottom) * 0.5

    sigma = 10.0
    u = np.mean(u, axis=2)
    v = np.mean(v, axis=2)
    h = (1.0 - u ** 2.0 - v ** 2.0).clip(0, 1e5) ** (0.5 * sigma)
    z = np.zeros_like(h)

    normal = np.stack([u, v, h], axis=2)
    normal /= np.sum(normal ** 2.0, axis=2, keepdims=True) ** 0.5
    normal = normal * matting + np.stack([z, z, 1 - z], axis=2) * (1 - matting)

    results = [normal, left, right, bottom, top] + inner_results
    results = [(x * 127.5 + 127.5).clip(0, 255).astype(np.uint8) for x in results]
    return results


quick_prompts = [
    'beautiful woman',
    'handsome man',
    'beautiful woman, cinematic lighting',
    'handsome man, cinematic lighting',
    'beautiful woman, natural lighting',
    'handsome man, natural lighting',
    'beautiful woman, neo punk lighting, cyberpunk',
    'handsome man, neo punk lighting, cyberpunk',
]
quick_prompts = [[x] for x in quick_prompts]


class BGSource(Enum):
    UPLOAD = "Use Background Image"
    UPLOAD_FLIP = "Use Flipped Background Image"
    LEFT = "Left Light"
    RIGHT = "Right Light"
    TOP = "Top Light"
    BOTTOM = "Bottom Light"
    GREY = "Ambient"
    CUSTOM_LEFT = "CUSTOM_LEFT"
    CUSTOM_LEFT_HIGH = "CUSTOM_LEFT_HIGH"
    CUSTOM_RIGHT = "CUSTOM_RIGHT"
    CUSTOM_RIGHT_HIGH = "CUSTOM_RIGHT_HIGH"
    CUSTOM_GRAY = "CUSTOM_GRAY"


# Setup argument parser  元のコードのblock以降を次のように変更（+ impoortにargparse追加）
parser = argparse.ArgumentParser(description="IC-Light (Relighting with Foreground Condition)")
parser.add_argument('--input_dir', type=str, required=True, help="Path to the input foreground dir")
parser.add_argument('--output_dir', type=str, required=True, help="Path to output dir")
parser.add_argument('--prompt', type=str, required=True, help="Prompt for text-to-image generation")
parser.add_argument('--bg_source', type=str, choices=[e.value for e in BGSource], default='None', help="Lighting Preference (Initial Latent)")
parser.add_argument('--num_samples', type=int, default=1, help="Number of generated images")
parser.add_argument('--seed', type=int, default=12345, help="Random seed")
parser.add_argument('--image_width', type=int, default=512, help="Width of the generated image")
parser.add_argument('--image_height', type=int, default=640, help="Height of the generated image")
parser.add_argument('--steps', type=int, default=25, help="Number of steps for generation")
parser.add_argument('--cfg', type=float, default=2.0, help="CFG Scale")
parser.add_argument('--highres_scale', type=float, default=1.5, help="Highres Scale")
parser.add_argument('--highres_denoise', type=float, default=0.5, help="Highres Denoise")
parser.add_argument('--a_prompt', type=str, default='best quality', help="Added prompt")
parser.add_argument('--n_prompt', type=str, default='fog, haze, faded, washed-out, lowres, bad anatomy, bad hands, cropped, worst quality, illustration, 3d, 2d, painting, cartoons, sketch, shadow, shade', help="Negative prompt")
parser.add_argument('--source_info_file', type=str, required=True, help="Path to the light source file")
parser.add_argument('--color_info_file', type=str, required=True, help="Path to the hair color file")

args = parser.parse_args()

# 出力フォルダが存在しない場合は作成
if not os.path.exists(args.output_dir):
    os.makedirs(args.output_dir)

# 指定フォルダ内の全ての画像ファイル名を取得
image_files = sorted(os.listdir(args.input_dir))
images = [f for f in image_files if f.endswith('.png')]

# 光源推定結果、髪色推定結果を読み込んで、各行をリストとして保持
with open(args.source_info_file, "r") as file:
    light_directions = [line.strip() for line in file]
with open(args.color_info_file, "r") as file:
    hair_colors = [line.strip() for line in file]

for image, light_source, hair_color in zip(images, light_directions, hair_colors):
    base_name = image.split('.')[0]
    image_path = os.path.join(args.input_dir, image)

    # Process function call with argparse arguments
    input_fg = np.array(Image.open(image_path))

    prompt = args.prompt
    if hair_color == "gray hair":
        prompt = "white hair " + prompt

    if light_source == "Left":
        bg_source = "CUSTOM_LEFT"
    elif light_source == "Left_High":
        bg_source = "CUSTOM_LEFT_HIGH"
    elif light_source == "Right":
        bg_source = "CUSTOM_RIGHT"
    elif light_source == "Right_High":
        bg_source = "CUSTOM_RIGHT_HIGH"
    else:
        bg_source = "CUSTOM_GRAY"

    bg_source = "CUSTOM_GRAY"  # use CUSTOM_GRAY for all estimated light direction

    results = process_relight(
        input_fg=input_fg,
        input_bg=None,
        prompt=args.prompt,
        image_width=args.image_width,
        image_height=args.image_height,
        num_samples=args.num_samples,
        seed=args.seed,
        steps=args.steps,
        a_prompt=args.a_prompt,
        n_prompt=args.n_prompt,
        cfg=args.cfg,
        highres_scale=args.highres_scale,
        highres_denoise=args.highres_denoise,
        bg_source=bg_source
    )

    # Save or display results
    for i, result in enumerate(results):
        result_img = Image.fromarray(result)
        if i==0:
            save_path = os.path.join(args.output_dir, image)
            result_img.save(save_path)

    print(f"Processing completed. Results saved as {image}_*.png")
