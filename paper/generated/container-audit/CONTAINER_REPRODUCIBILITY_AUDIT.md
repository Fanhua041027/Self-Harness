# 任务容器基础镜像可复现性审计

本审计只读取任务 Dockerfile，不读取模型输出或评测结果，也不拉取镜像。

- 任务数：89。
- `FROM` 引用数：90。
- 所有基础镜像均使用 digest 的任务：0 / 89。
- Digest-pinned 引用：0；显式 tag：90；浮动/latest：0；变量：0。

## 基础镜像引用

| Reference | Uses | Pinning |
|---|---:|---|
| `python:3.13-slim-bookworm` | 41 | explicit_tag |
| `ubuntu:24.04` | 40 | explicit_tag |
| `debian:13.0-slim` | 2 | explicit_tag |
| `debian:bullseye-slim` | 2 | explicit_tag |
| `python:3.10-slim-bookworm` | 2 | explicit_tag |
| `python:3.11-slim` | 2 | explicit_tag |
| `python:3.11` | 1 | explicit_tag |

解释边界：显式版本 tag 比 `latest` 更稳定，但 tag 仍可被 registry 重新指向，不能提供内容不可变性。只有 `@sha256:` digest 能静态证明基础镜像内容身份。任务目录 SHA256 可以证明 Dockerfile 未变，不能证明其 tag 在不同时间解析到相同镜像。正式运行应记录实际解析的 image ID/RepoDigest；由于当前 Sealed21 尚未执行，本报告不事后改写冻结任务 Dockerfile。
