# References — speech interface (STT / TTS)

Papers supporting each design choice in `speech/`, for the project report. All were checked
against the publisher's page or the full text; PDFs are not committed (publisher copyright) —
use the links below.

## Design choice → evidence

| Design choice | Paper | What it supports |
|---|---|---|
| STT with Whisper (`stt.py`) | Radford et al. (2023) | Trained on 680,000 hours of weakly supervised web audio; generalises to standard benchmarks without dataset-specific fine-tuning — suits open-ended everyday conversation with no training of our own. |
| Local whisper.cpp with q5-quantized models, model picked by hardware | Andreyev (2025) *(preprint)* | Evaluates INT4/INT5/INT8 quantization of Whisper in whisper.cpp: ~19% lower latency and ~45% smaller models while preserving accuracy, enabling edge deployment. Matches our own measurements (README: q5 models same speed/accuracy at about a third of the size). |
| TTS with Piper (`tts.py`) | Kim, Kong & Son (2021) | Piper's voices are VITS models (its training code lives under `piper_train/vits/`). VITS is a single-stage, parallel end-to-end TTS model that outperformed two-stage systems, with naturalness (MOS) close to ground-truth recordings on LJ Speech. |
| A distinct voice per character; speaking rate from the character's OCEAN extraversion (`voices.json`) | Nass & Lee (2001) | People recognise personality in synthesized speech from vocal parameters alone, and respond to it socially. Extrovert voice: 216 words/min, max volume, 140 Hz fundamental frequency, 40 Hz range; introvert: 184 words/min, 15% volume, 84 Hz, 16 Hz range. Users preferred voices consistent with their own personality and with the content (similarity- and consistency-attraction). |
| Several selectable robot personas rather than one fixed personality | Lee, Peng, Jin & Yan (2006) | With Sony's AIBO, participants accurately recognised an introvert vs. extrovert robot from its verbal and nonverbal behaviour — but preferred robots *complementary* to their own personality (opposite to Nass & Lee's similarity effect for voices). Since which personality a user prefers varies, offering multiple characters, each with a consistent voice, is a reasonable design. |
| *(future)* Emotion-dependent speaking style, e.g. from LLM2's angry/sad/happy output | Wang et al. (2018) | Global Style Tokens let an end-to-end TTS model control speaking style and speed independently of the text content, and transfer styles. Basis for adding expressive/emotional TTS later; not implemented. |

Notes for the report:
- Our speed mapping (`extraversion_length_scale`: Low 1.15 / Medium 1.0 / High 0.9) follows the
  direction of Nass & Lee (extroverts speak faster); the paper's own ratio is 216/184 ≈ 1.17.
  Piper offers rate and voice choice but no pitch control, so pitch/frequency range — the other
  cues in Nass & Lee — are only varied through the choice of voice.
- Andreyev (2025) is an arXiv preprint (not peer reviewed); cite it as such, alongside our own
  measurements.

## Full references (APA 7)

Andreyev, A. (2025). *Quantization for OpenAI's Whisper models: A comparative analysis*
(arXiv:2503.09905) [Preprint]. arXiv. https://doi.org/10.48550/arXiv.2503.09905
— PDF: https://arxiv.org/pdf/2503.09905 · code: https://github.com/allisonandreyev/WhisperQuantization

Kim, J., Kong, J., & Son, J. (2021). Conditional variational autoencoder with adversarial
learning for end-to-end text-to-speech. In *Proceedings of the 38th International Conference on
Machine Learning* (Vol. 139, pp. 5530–5540). PMLR.
https://proceedings.mlr.press/v139/kim21f.html
— PDF: https://proceedings.mlr.press/v139/kim21f/kim21f.pdf · arXiv: https://arxiv.org/abs/2106.06103

Lee, K. M., Peng, W., Jin, S.-A., & Yan, C. (2006). Can robots manifest personality?: An
empirical test of personality recognition, social responses, and social presence in
human–robot interaction. *Journal of Communication, 56*(4), 754–772.
https://doi.org/10.1111/j.1460-2466.2006.00318.x
— full text via the University library (publisher: Oxford University Press)

Nass, C., & Lee, K. M. (2001). Does computer-synthesized speech manifest personality?
Experimental tests of recognition, similarity-attraction, and consistency-attraction.
*Journal of Experimental Psychology: Applied, 7*(3), 171–181.
https://doi.org/10.1037/1076-898X.7.3.171
— PDF (APA): https://www.apa.org/pubs/journals/releases/xap-73171.pdf

Radford, A., Kim, J. W., Xu, T., Brockman, G., McLeavey, C., & Sutskever, I. (2023). Robust
speech recognition via large-scale weak supervision. In *Proceedings of the 40th International
Conference on Machine Learning* (Vol. 202, pp. 28492–28518). PMLR.
https://proceedings.mlr.press/v202/radford23a.html
— PDF: https://proceedings.mlr.press/v202/radford23a/radford23a.pdf · arXiv: https://arxiv.org/abs/2212.04356

Wang, Y., Stanton, D., Zhang, Y., Skerry-Ryan, R. J., Battenberg, E., Shor, J., Xiao, Y.,
Jia, Y., Ren, F., & Saurous, R. A. (2018). Style tokens: Unsupervised style modeling, control
and transfer in end-to-end speech synthesis. In *Proceedings of the 35th International
Conference on Machine Learning* (Vol. 80, pp. 5180–5189). PMLR.
https://proceedings.mlr.press/v80/wang18h.html
— PDF: https://proceedings.mlr.press/v80/wang18h/wang18h.pdf · arXiv: https://arxiv.org/abs/1803.09017
