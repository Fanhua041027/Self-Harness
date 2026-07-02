# Self-Harness: Harnesses That Improve Themselves

<p align="center">
  <a href="https://arxiv.org/abs/2606.09498">
    <img src="https://img.shields.io/badge/arXiv-2606.09498-b31b1b.svg" alt="arXiv:2606.09498">
  </a>
</p>

<p align="center">
  <img src="assets/figure1.png" alt="Figure 1: Three paradigms of harness improvement" width="900">
</p>

## Overview

Self-Harness keeps the model weights and evaluator fixed while improving the
surrounding harness. Each round evaluates the current harness on tasks, mines
failure patterns from execution traces, asks the same model to propose bounded
harness edits, and promotes a candidate only when held-in and held-out
regression checks support the change.

<p align="center">
  <img src="assets/overview.png" alt="Overview of the Self-Harness optimization loop" width="900">
</p>

## Results

Terminal-Bench-2.0 pass rates (%). More results are coming soon.

| Model | Initial | Self-Harness |
| --- | ---: | ---: |
| MiniMax M2.5 | 42.2 | 53.9 |
| Qwen3.5-35B-A3B | 18.0 | 36.7 |
| GLM-5 | 46.1 | 57.0 |

## Citation

```bibtex
@misc{zhang2026selfharnessharnessesimprove,
      title={Self-Harness: Harnesses That Improve Themselves},
      author={Hangfan Zhang and Shao Zhang and Kangcong Li and Chen Zhang and Yang Chen and Yiqun Zhang and Lei Bai and Shuyue Hu},
      year={2026},
      eprint={2606.09498},
      archivePrefix={arXiv},
      primaryClass={cs.CL},
      url={https://arxiv.org/abs/2606.09498},
}
```
