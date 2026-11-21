# Bundled enhancement models and runtime

Clipforge bundles two ESPCN super-resolution models and runs them through
OpenCV's `dnn_superres` module.

| Asset | Source | Licence |
| --- | --- | --- |
| `ESPCN_x2.pb` | https://github.com/fannymonori/TF-ESPCN (`export/`) | Apache-2.0 |
| `ESPCN_x4.pb` | https://github.com/fannymonori/TF-ESPCN (`export/`) | Apache-2.0 |
| OpenCV contrib headless | https://github.com/opencv/opencv_contrib | Apache-2.0 |

The full ESPCN licence text is in `LICENSE-ESPCN.txt`. The model paper is
Shi et al., "Real-Time Single Image and Video Super-Resolution Using an
Efficient Sub-Pixel Convolutional Neural Network" (CVPR 2016).

The optional Real-ESRGAN ONNX weights are **not** bundled. They are downloaded
only after an explicit user action, from the pinned URL in
`clipforge/enhancement/settings.py`, and are BSD-3-Clause. Upstream:
https://github.com/xinntao/Real-ESRGAN.
