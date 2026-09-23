# Third-party components

What the shipped apps bundle beyond this project's own code, with the
license each one carries. Model weights are downloaded on first use rather
than bundled, but they are listed here too because the app fetches them on
the user's behalf.

## Runtimes

| Component | Used for | License |
|---|---|---|
| [PyTorch](https://pytorch.org/) + torchvision | The torch inference backend (CUDA / ROCm / MPS / DirectML / CPU) | BSD-3-Clause |
| [spandrel](https://github.com/chaiNNer-org/spandrel) | Loading the torch models' weights | MIT |
| [ncnn](https://github.com/Tencent/ncnn) | The Vulkan inference backend ("Vulkan Models") | BSD-3-Clause |
| [MoltenVK](https://github.com/KhronosGroup/MoltenVK) | Vulkan on macOS for the ncnn backend (bundled in the macOS apps only; `packaging/ncnn/molten-vk.env` pins the release) | Apache-2.0 |
| torch-directml | AMD/Intel GPUs on Windows (the `directml` build only) | MIT |

## Upscale models — torch backend

| Model id | Author | License |
|---|---|---|
| `ultrasharp_v2`, `ultrasharp_v2_lite` | [Kim2091](https://huggingface.co/Kim2091/UltraSharpV2) | CC-BY-NC-SA-4.0 |
| `illustrationjanai` | the-database (IllustrationJaNai) | CC-BY-NC-SA-4.0 |
| `realesrgan_anime_fast` | [Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN) (Xintao Wang et al.) | BSD-3-Clause |

## Upscale models — Vulkan (ncnn) backend

The ncnn `.param`/`.bin` pairs are hosted at `dl.proxy-scaler.com/models/ncnn/`,
either the author's own ncnn release or our conversion of the author's
PyTorch release (`packaging/ncnn/convert-models.py`, sources and hashes in
`packaging/ncnn/models.toml`). Converted files are derivatives and carry
the original model's license.

| Model id | Author | License |
|---|---|---|
| `realesrgan_anime_fast_vk` | [Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN) (Xintao Wang et al.), official ncnn release | BSD-3-Clause |
| `realesrgan_anime6b_vk` | [Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN) (Xintao Wang et al.), official ncnn release | BSD-3-Clause |
| `ultrasharp_v1_vk` | [Kim2091](https://huggingface.co/Kim2091/UltraSharp), author's own ncnn release | CC-BY-NC-SA-4.0 |
| `clearreality_v1_vk` | [Kim2091](https://openmodeldb.info/models/4x-ClearRealityV1), our conversion (pnnx) | CC-BY-NC-SA-4.0 |
| `animesharp_vk` | [Kim2091](https://openmodeldb.info/models/4x-AnimeSharp), our conversion (esrgan2ncnn) | CC-BY-NC-SA-4.0 |
| `nomos8ksc_vk` | [Philip Hofmann](https://openmodeldb.info/models/4x-Nomos8kSC), our conversion (esrgan2ncnn) | CC-BY-4.0 |
| `nomosuni_span_vk` | [Philip Hofmann](https://openmodeldb.info/models/4x-NomosUni-span-multijpg), our conversion (pnnx) | CC-BY-4.0 |
| `hfa2k_vk` | [Philip Hofmann](https://openmodeldb.info/models/4x-HFA2k), our conversion (esrgan2ncnn) | CC-BY-4.0 |
