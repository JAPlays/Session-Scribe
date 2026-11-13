# Offline installers

`build.ps1` turns this repository into one Windows installer that has everything inside:

| Part | What it is |
|---|---|
| Session Scribe | `session_scribe.py`, `engine_check.py` |
| Python 3.12 | A private copy with tkinter, WhisperX, PyTorch (CPU), cryptography, pypdf, the theme |
| Built-in AI | llama.cpp's `llama-server` (Vulkan build: works on Intel/AMD integrated graphics and on NVIDIA/AMD cards) |
| AI model | The edition's GGUF model, e.g. Mistral Small 3.1 24B (Q4_K_M) |
| Speech | The Whisper model, the word-alignment model, the pyannote speaker model, FFmpeg |

Installing needs no internet, no LM Studio, no Python and no Hugging Face token. On first start, the app
checks the computer (memory, processor, graphics) and fits itself to it (see **How the app fits itself
to a computer** below). The only setup left is the app's own first-run setup: choose a data folder and
a vault password.

## Editions

There's one installer per kind of computer. Each one is the same program with a different AI model
inside, defined in `editions.json`:

| Edition | Model | For |
|---|---|---|
| `mistral-24b` | Mistral Small 3.1 24B, Q4_K_M (~14 GB) | 32 GB of memory or more. Integrated graphics works, though notes are slow; a 16 GB+ graphics card is fast |

To add an edition for smaller computers, copy the `mistral-24b` entry, give it a new key, a GGUF
repository on Hugging Face (`repo`) and a quantization (`quant`), and lower `min_ram_gb`. Then build it
with `-Edition <key>`. All editions install to the same place and replace one another, so a computer has
one at a time. Patient data is never touched.

## Building (on a Windows PC, once per edition)

1. Install [Inno Setup 6](https://jrsoftware.org/isdl.php).
2. For speaker labels: on huggingface.co, accept the terms of the pyannote speaker models that WhisperX
   uses (`pyannote/speaker-diarization-community-1`, or `speaker-diarization-3.1` and
   `segmentation-3.0` on older WhisperX). Then create a read token.
3. In PowerShell, from this folder:
   ```
   $env:HF_TOKEN = "hf_..."
   powershell -ExecutionPolicy Bypass -File .\build.ps1 -Edition mistral-24b
   ```

The first build downloads about 20 GB into `build\cache`. Later builds reuse it. The token is only used
for that download and isn't put in the installer.

The result is in `dist\`: `SessionScribe-mistral-24b-<build>-setup.exe` plus several `.bin` parts. A
Windows program can't be larger than 4 GB, so the installer comes in pieces. Keep them in one folder,
and to hand it to someone, copy the whole folder (a 32 GB USB drive is enough).

The engine and Python versions are pinned: llama.cpp `b11900` and Python 3.12.16 (python-build-standalone
`20261026`), both from 26 October 2026. To change them, pass `-LlamaTag`, or `-PythonTag` with
`-PythonVersion`. `-LlamaTag latest` and `-PythonTag latest` use the newest release instead.
`bundle.json` in the installed folder records which versions went in.

## How the app fits itself to a computer

On the first start, and from **Settings › This computer › Check this computer again**, the app looks at:

- memory (total and free), processor and cores,
- the graphics devices `llama-server` can use (`--list-devices`), and whether each is integrated,
- each AI model's size and shape, read from its GGUF header (layers, context-cache size).

From that it picks:

- **AI model:** the edition's model, if this computer can run it. Otherwise the largest model that fits.
- **Context length:** 16K, shortened to 12K or 8K if memory is tight.
- **Graphics offload:**
  - On a dedicated graphics card, as many layers as the card holds.
  - On integrated graphics, as many as the share of memory the graphics driver offers. If the graphics
    driver can't load the model, the app falls back to the processor and remembers that.
- **Speech model, batch size and threads:** `large-v3-turbo` on capable computers, smaller models on
  small ones, with one thread per processor core.

Settings lists every model with **Fits well / Slow here / Won't fit** and one line on why. Picking another
model takes effect from the next AI step.

To add your own models, put more `.gguf` files (split ones too) in `%LOCALAPPDATA%\Session Scribe\models`.

While transcribing, if free memory is low, the app unloads the AI model and loads it again for the notes.
The built-in AI only listens on 127.0.0.1 and closes with the app.

## Licenses

`THIRD-PARTY-NOTICES.txt` lists what's inside and its license, and the installer shows it. The parts are
MIT, BSD or Apache-2.0, plus FFmpeg's LGPL/GPL and the pyannote model terms. That allows handing the
installer to other people. Check the license of any model you add to an edition.
