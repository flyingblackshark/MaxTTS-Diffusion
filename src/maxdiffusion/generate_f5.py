"""
 Copyright 2025 Google LLC

 Licensed under the Apache License, Version 2.0 (the "License");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at

      https://www.apache.org/licenses/LICENSE-2.0

 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an "AS IS" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
"""

from typing import Callable, List, Union, Sequence
from absl import app
from contextlib import ExitStack
import functools
import jax.experimental
import jax.experimental.compilation_cache.compilation_cache
import jax.experimental.ode
import numpy as np
import jax
from jax.sharding import Mesh, PositionalSharding, PartitionSpec as P
import jax.numpy as jnp
import flax.linen as nn
from chex import Array
from einops import rearrange
from flax.linen import partitioning as nn_partitioning
import flax
import re
from pypinyin import lazy_pinyin, Style
import jieba
from maxdiffusion import pyconfig, max_logging
from maxdiffusion.models.f5.transformers.transformer_f5_flax import F5TextEmbedding, F5Transformer2DModel
from maxdiffusion.max_utils import (
    device_put_replicated,
    get_memory_allocations,
    create_device_mesh,
    get_flash_block_sizes,
    get_precision,
    setup_initial_state,
)
import time
from maxdiffusion.models.modeling_flax_pytorch_utils import convert_f5_state_dict_to_flax
import os
from importlib.resources import files
import librosa
import audax.core.functional
import jax.experimental.compilation_cache
jax.experimental.compilation_cache.compilation_cache.set_cache_dir("./jax_cache")
cfg_strength = 2
def loop_body(
    step,
    args,
    transformer,
    cond,
    decoder_segment_ids,
    text_embed_cond,
    text_embed_uncond,
):
    latents,state, c_ts, p_ts = args
    latents_dtype = latents.dtype
    t_curr = c_ts[step]
    t_prev = p_ts[step]
    t_vec = jnp.full((latents.shape[0],), t_curr, dtype=latents.dtype)
    pred = transformer.apply(
        {"params": state.params},
        x=latents,
        cond=cond,
        decoder_segment_ids=decoder_segment_ids,
        text_embed=text_embed_cond,
        timestep=t_vec,
    )
    null_pred = transformer.apply(
        {"params": state.params},
        x=latents,
        cond=jnp.zeros_like(cond),
        decoder_segment_ids=decoder_segment_ids,
        text_embed=text_embed_uncond,
        timestep=t_vec,
        #drop_audio_cond=True,
    )
    pred = pred + (pred - null_pred) * cfg_strength
    latents = latents + (t_prev - t_curr) * pred
    latents = jnp.array(latents, dtype=latents_dtype)
    return latents, state, c_ts, p_ts

def run_inference(
    states, transformer, config, mesh, latents, cond, decoder_segment_ids,text_embed_cond,text_embed_uncond, c_ts, p_ts
):

  transformer_state = states


  loop_body_p = functools.partial(
      loop_body,
      transformer=transformer,
      cond=cond,
      decoder_segment_ids=decoder_segment_ids,
      text_embed_cond=text_embed_cond,
      text_embed_uncond=text_embed_uncond,
  )

  with mesh, nn_partitioning.axis_rules(config.logical_axis_rules):
    latents, _, _, _ = jax.lax.fori_loop(0, len(c_ts), loop_body_p, (latents, transformer_state, c_ts, p_ts))

  return latents

def run(config):
  
    rng = jax.random.key(config.seed)
    devices_array = create_device_mesh(config)
    mesh = Mesh(devices_array, config.mesh_axes)

    #global_batch_size = config.per_device_batch_size * jax.local_device_count()


    # LOAD TRANSFORMER
    flash_block_sizes = get_flash_block_sizes(config)
    transformer = F5Transformer2DModel(
        mesh=mesh,
        mlp_ratio=2,
        #split_head_dim=config.split_head_dim,
        attention_kernel=config.attention,
        flash_block_sizes=flash_block_sizes,
        dtype=config.activations_dtype,
        weights_dtype=config.weights_dtype,
        precision=get_precision(config),
    )
    transformer_params,text_encoder_params = convert_f5_state_dict_to_flax(config.pretrained_model_name_or_path,use_ema=config.use_ema)
    weights_init_fn = functools.partial(transformer.init_weights, rngs=rng, max_sequence_length=config.max_sequence_length, eval_only=False)
    transformer_state, transformer_state_shardings = setup_initial_state(
        model=transformer,
        tx=None,
        config=config,
        mesh=mesh,
        weights_init_fn=weights_init_fn,
        model_params=None,
        training=False,
    )
    transformer_state = transformer_state.replace(params=transformer_params)
    transformer_state = jax.device_put(transformer_state, transformer_state_shardings)
    get_memory_allocations()
    def dynamic_range_compression_jax(x, C=1, clip_val=1e-7):
        return jnp.log(jnp.clip(x,min=clip_val) * C)

    def get_mel(y, n_mels=100,n_fft=1024,win_size=1024,hop_length=256,fmin=0,fmax=None,clip_val=1e-7,sampling_rate=24000):
        window = jnp.hanning(win_size)
        spec_func = functools.partial(audax.core.functional.spectrogram, pad=0, window=window, n_fft=n_fft,
                        hop_length=hop_length, win_length=win_size, power=1.,
                        normalized=False, center=True, onesided=True)
        fb = audax.core.functional.melscale_fbanks(n_freqs=(n_fft//2)+1, n_mels=n_mels,
                            sample_rate=sampling_rate, f_min=fmin, f_max=fmax)
        mel_spec_func = functools.partial(audax.core.functional.apply_melscale, melscale_filterbank=fb)
        spec = spec_func(y)
        spec = mel_spec_func(spec)
        spec = dynamic_range_compression_jax(spec, clip_val=clip_val)
        return spec

    
    def convert_char_to_pinyin(text_list, polyphone=True):
        if jieba.dt.initialized is False:
            jieba.default_logger.setLevel(50)  # CRITICAL
            jieba.initialize()

        final_text_list = []
        custom_trans = str.maketrans(
            {";": ",", "“": '"', "”": '"', "‘": "'", "’": "'"}
        )  # add custom trans here, to address oov

        def is_chinese(c):
            return (
                "\u3100" <= c <= "\u9fff"  # common chinese characters
            )

        for text in text_list:
            char_list = []
            text = text.translate(custom_trans)
            for seg in jieba.cut(text):
                seg_byte_len = len(bytes(seg, "UTF-8"))
                if seg_byte_len == len(seg):  # if pure alphabets and symbols
                    if char_list and seg_byte_len > 1 and char_list[-1] not in " :'\"":
                        char_list.append(" ")
                    char_list.extend(seg)
                elif polyphone and seg_byte_len == 3 * len(seg):  # if pure east asian characters
                    seg_ = lazy_pinyin(seg, style=Style.TONE3, tone_sandhi=True)
                    for i, c in enumerate(seg):
                        if is_chinese(c):
                            char_list.append(" ")
                        char_list.append(seg_[i])
                else:  # if mixed characters, alphabets and symbols
                    for c in seg:
                        if ord(c) < 256:
                            char_list.extend(c)
                        elif is_chinese(c):
                            char_list.append(" ")
                            char_list.extend(lazy_pinyin(c, style=Style.TONE3, tone_sandhi=True))
                        else:
                            char_list.append(c)
            final_text_list.append(char_list)

        return final_text_list
    def get_tokenizer(dataset_name, tokenizer: str = "pinyin"):
        """
        tokenizer   - "pinyin" do g2p for only chinese characters, need .txt vocab_file
                    - "char" for char-wise tokenizer, need .txt vocab_file
                    - "byte" for utf-8 tokenizer
                    - "custom" if you're directly passing in a path to the vocab.txt you want to use
        vocab_size  - if use "pinyin", all available pinyin types, common alphabets (also those with accent) and symbols
                    - if use "char", derived from unfiltered character & symbol counts of custom dataset
                    - if use "byte", set to 256 (unicode byte range)
        """
        if tokenizer in ["pinyin", "char"]:
            tokenizer_path = os.path.join(files("f5_tts").joinpath("../../data"), f"{dataset_name}_{tokenizer}/vocab.txt")
            with open(tokenizer_path, "r", encoding="utf-8") as f:
                vocab_char_map = {}
                for i, char in enumerate(f):
                    vocab_char_map[char[:-1]] = i
            vocab_size = len(vocab_char_map)
            assert vocab_char_map[" "] == 0, "make sure space is of idx 0 in vocab.txt, cuz 0 is used for unknown char"

        elif tokenizer == "byte":
            vocab_char_map = None
            vocab_size = 256

        elif tokenizer == "custom":
            with open(dataset_name, "r", encoding="utf-8") as f:
                vocab_char_map = {}
                for i, char in enumerate(f):
                    vocab_char_map[char[:-1]] = i
            vocab_size = len(vocab_char_map)

        return vocab_char_map, vocab_size

    def chunk_text(text, max_chars=135):
        """
        Splits the input text into chunks, each with a maximum number of characters.

        Args:
            text (str): The text to be split.
            max_chars (int): The maximum number of characters per chunk.

        Returns:
            List[str]: A list of text chunks.
        """
        chunks = []
        current_chunk = ""
        # Split the text into sentences based on punctuation followed by whitespace
        sentences = re.split(r"(?<=[;:,.!?])\s+|(?<=[；：，。！？])", text)

        for sentence in sentences:
            if len(current_chunk.encode("utf-8")) + len(sentence.encode("utf-8")) <= max_chars:
                current_chunk += sentence + " " if sentence and len(sentence[-1].encode("utf-8")) == 1 else sentence
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                current_chunk = sentence + " " if sentence and len(sentence[-1].encode("utf-8")) == 1 else sentence

        if current_chunk:
            chunks.append(current_chunk.strip())

        return chunks
    num_devices = len(jax.devices())
    data_sharding = jax.sharding.NamedSharding(mesh, P(*config.data_sharding))
    #data_sharding = jax.sharding.NamedSharding(mesh, P(("data", "fsdp"), "sequence"))
    batch_size = 3 * num_devices
    local_speed = 1
    max_duration = 4096
    ref_text = "and there are so many things about humankind that is bad and evil. I strongly believe that love is one of the only things we have in this world."
    if len(ref_text[-1].encode("utf-8")) == 1:
        ref_text = ref_text + " "
    gen_text = "Hello,I'm Aurora.And nice to meet you.This is a very long sentence intended to test the stability of the model.I really like this model and so I use it a lot."
    #gen_text = "The impact of technology on modern society is profound, influencing nearly every aspect of daily life, from communication to healthcare, education, and business. The rapid advancements in artificial intelligence, automation, and digital connectivity have transformed the way people interact, work, and access information. Social media platforms have redefined communication, enabling instant global connections but also raising concerns about privacy, mental health, and misinformation. In the workplace, automation and AI-driven tools have increased efficiency and productivity while simultaneously reshaping job markets, requiring individuals to continuously adapt and acquire new skills. In education, online learning platforms and digital resources have made knowledge more accessible, bridging gaps in traditional education systems but also highlighting issues of digital divide and screen dependency. Healthcare has seen groundbreaking innovations such as telemedicine, wearable health monitors, and AI-assisted diagnostics, improving patient care but also posing ethical and regulatory challenges. Despite these advancements, concerns about cybersecurity, data privacy, and the ethical implications of AI remain pressing issues. As technology continues to evolve, balancing innovation with ethical considerations and ensuring equitable access to its benefits will be crucial for a sustainable and inclusive future. Ultimately, while technology offers immense potential to improve lives, its responsible and mindful use is essential to mitigating its challenges."
    ref_audio, ref_sr = librosa.load("/root/MaxTTS-Diffusion/test.mp3",sr=24000)
    max_chars = int(len(ref_text.encode("utf-8")) / (ref_audio.shape[-1] / ref_sr) * (22 - ref_audio.shape[-1] / ref_sr))
    vocab_char_map, vocab_size = get_tokenizer(config.vocab_name_or_path, "custom")
    gen_text_batches = chunk_text(gen_text, max_chars=max_chars)
    batched_text_list = []
    batched_duration = []
    ref_max_length = max_duration * 256 
    ref_audio_len = ref_audio.shape[-1] // 256 + 1
    for single_gen_text in gen_text_batches:
        text_list = ref_text + single_gen_text
        ref_text_len = len(ref_text.encode("utf-8"))
        gen_text_len = len(single_gen_text.encode("utf-8"))
        duration = ref_audio_len + int(ref_audio_len / ref_text_len * gen_text_len / local_speed)
        batched_duration.append(duration)
        batched_text_list.append(text_list)
    final_text_list = convert_char_to_pinyin(batched_text_list)
    
    def list_str_to_idx(
        text: list[str] | list[list[str]],
        vocab_char_map: dict[str, int],  # {char: idx}
        #padding_value=-1,
    ):  # noqa: F722
        outs = []
        for t in text:
            list_idx_tensors = [vocab_char_map.get(c, 0) for c in t]  # pinyin or char style
            text_ids = jnp.asarray(list_idx_tensors)
            #text = pad_sequence(list_idx_tensors, padding_value=padding_value, batch_first=True)
            text_ids = text_ids + 1
            text_ids = jnp.pad(text_ids, ((0, max_duration - text_ids.shape[-1])))
            outs.append(text_ids)
        stacked_text_idss = jnp.stack(outs)
        return stacked_text_idss
    text_ids = list_str_to_idx(final_text_list, vocab_char_map)
    padded_batch_size = batch_size - text_ids.shape[0]
    text_ids = jnp.pad(text_ids, ((0,padded_batch_size),(0,0)))

    ref_audio = jnp.pad(ref_audio,(0,ref_max_length - 256 - ref_audio.shape[0]))
    
    ref_audio = jax.device_put(ref_audio[np.newaxis,:],jax.sharding.NamedSharding(mesh, P(None, "data")))
    

    
    

    rng = {'params': jax.random.PRNGKey(0), 'dropout': jax.random.PRNGKey(0)}

    def lens_to_mask(t: jnp.ndarray, length: int | None = None) -> jnp.ndarray:
        if length is None:
            length = jnp.max(t)  # 使用t的最大值作为默认长度
        
        # 创建从0到length-1的序列
        seq = jnp.arange(length)
        
        # 广播比较：每个元素t[i]与序列的每个位置比较
        mask = seq < t[:, None]  # 形状: (b, n)
        
        return mask
    

    lens = jnp.full((batch_size,), ref_audio_len)
    duration = jnp.asarray(batched_duration)
    duration = jnp.pad(duration,(0,padded_batch_size))
    duration = jnp.maximum(jnp.maximum((text_ids != 0).sum(axis=-1), lens) + 1, duration) 

    cond_mask = lens_to_mask(lens,length=config.max_sequence_length)
    mask = lens_to_mask(duration,length=config.max_sequence_length)

    cond = jax.jit(get_mel,out_shardings=None)(ref_audio)
    cond_mask = jnp.pad(cond_mask, ((0,batch_size-cond_mask.shape[0]),(0, max_duration - cond_mask.shape[-1])), constant_values=False)
    mask = jnp.pad(mask, ((0,batch_size-mask.shape[0]),(0, max_duration - mask.shape[-1])), constant_values=False)
    
    text_decoder_segment_ids = (text_ids != 0).astype(jnp.int32)
    decoder_segment_ids = mask.astype(jnp.int32)

    text_encoder = F5TextEmbedding(text_num_embeds=2545,text_dim=512,conv_layers=4)
    jitted_text_encode = jax.jit(text_encoder.apply,out_shardings=None)

    step_cond = jnp.where(
        cond_mask[...,jnp.newaxis], cond, jnp.zeros_like(cond)
    ) 

     
    latents = jax.random.normal(jax.random.PRNGKey(0), (batch_size,max_duration,100))
    latents = jax.device_put(latents, data_sharding)
    step_cond = jax.device_put(step_cond, data_sharding)
    text_ids = jax.device_put(text_ids, data_sharding)

    t_start = 0
    timesteps = jnp.linspace(t_start, 1.0, config.num_inference_steps + 1).astype(jnp.float32)
    timesteps = timesteps + config.sway_sampling_coef * (jnp.cos(jnp.pi / 2 * timesteps) - 1 + timesteps) # sway sampling
    c_ts = timesteps[:-1]
    p_ts = timesteps[1:]

    text_embed_cond = jitted_text_encode({"params":text_encoder_params},
                                    text=text_ids,
                                    #seq_len=config.max_sequence_length,
                                    #decoder_segment_ids=decoder_segment_ids,
                                    text_decoder_segment_ids=text_decoder_segment_ids,
                                    rngs=rng)
    text_embed_uncond = jitted_text_encode({"params":text_encoder_params},
                                text=jnp.zeros_like(text_ids),
                                #seq_len=config.max_sequence_length,
                                #decoder_segment_ids=decoder_segment_ids,
                                text_decoder_segment_ids=text_decoder_segment_ids,
                                rngs=rng)
    
    p_run_inference = jax.jit(
    functools.partial(
        run_inference,
        transformer=transformer,
        config=config,
        mesh=mesh,
        latents=latents,
        cond=step_cond,
        decoder_segment_ids=decoder_segment_ids,
        text_embed_cond=text_embed_cond,
        text_embed_uncond=text_embed_uncond,
        c_ts=c_ts,
        p_ts=p_ts,
    ),
    in_shardings=(transformer_state_shardings,),
    out_shardings=None,
    )

    y_final = p_run_inference(transformer_state)
    out = y_final
    out = jnp.where(cond_mask[...,jnp.newaxis], cond, out)
    from jax_vocos import load_model
    vocos_model,vocos_params = load_model()
    rng = {'params': jax.random.PRNGKey(0), 'dropout': jax.random.PRNGKey(0)}

    out = jax.device_put(out, data_sharding)
    res = jax.jit(vocos_model.apply,out_shardings=None)({"params":vocos_params},out,rngs=rng)

    import soundfile as sf
    
    t0 = time.perf_counter()
    
    res_cpu = np.asarray(res)
    output_segment = res_cpu[0][ref_audio_len*256:duration[0]*256]
    for i in range(batch_size - padded_batch_size):
        output_segment = np.concatenate((output_segment,res_cpu[i+1][ref_audio_len*256:duration[i+1]*256]))
    sf.write("output.wav",output_segment,samplerate=24000)
    t1 = time.perf_counter()
    max_logging.log(f"transfer to cpu first and slice time: {t1 - t0:.1f}s.")

    return None


def main(argv: Sequence[str]) -> None:
  pyconfig.initialize(argv)
  run(pyconfig.config)


if __name__ == "__main__":
  app.run(main)
