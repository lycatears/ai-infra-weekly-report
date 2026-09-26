# AI Infra 周报 · 2026-09-25

> **本期概览**
>
> - **覆盖时间**：2026-09-21 00:00 ~ 2026-09-26 02:15（Asia/Shanghai）
> - **生成时间**：2026-09-26 02:17:11
> - **生成模型**：deepseek-flash
> - **数据源**：arXiv 66、GitHub 2、Hacker News 87、官方博客 RSS 51、搜索 API 19
> - **筛选过程**：抓取 225 条 → 去重后 223 条 → 关键词过滤掉 153 条 → 初筛选中 30 条 → **本期收录 15 条**
> - **Token 用量**：34,416（输入 16,535 / 输出 17,881）

---

## 1. vLLM

- **项目名称**：vLLM
- **发表时间**：2026-09-22 13:20（Asia/Shanghai · UTC+08:00） · 来源：GitHub
- **一句话总结**：vLLM 发布新版本，762 次提交，新增 DeepSeek-V4.1-Flash 等模型支持。
- **项目亮点**：
  - 762 次提交，315 位贡献者，含 104 位新贡献者
  - 新增 DeepSeek-V4.1-Flash，KV Cache 全量存为 MXFP8
  - FlashMLA V4.1 记录在 SM100 上落地
  - 新增 DeepGEMM Mega-mHC 与 Engram DP 分片异步预取
- **关键字**：`vLLM`、`KV Cache`、`MXFP8`、`FlashMLA`、`DeepSeek`、`DeepGEMM`
- **相关链接**：
  - <https://github.com/vllm-project/vllm/releases/tag/v0.30.0>

---

## 2. NVIDIA TensorRT

- **项目名称**：NVIDIA TensorRT
- **发表时间**：2026-09-22 05:51（Asia/Shanghai · UTC+08:00） · 来源：官方博客 RSS
- **一句话总结**：TensorRT 新增多设备推理能力，并集成到 Dynamo-Triton。
- **项目亮点**：
  - 生成式 AI 的算力与显存需求已超出单 GPU 供给
  - TensorRT 多设备推理（multi-device）为新能力
  - 在 NVIDIA Dynamo-Triton 中完成集成
  - 目标是简化跨多 GPU 的模型服务部署
- **关键字**：`TensorRT`、`Triton`、`multi-device inference`、`model serving`、`GPU memory`
- **相关链接**：
  - <https://developer.nvidia.com/blog/simplifying-model-serving-across-multiple-gpus-with-nvidia-tensorrt-multi-device-integration-in-nvidia-dynamo-triton/>

---

## 3. Crossflow

- **项目名称**：Crossflow
- **发表时间**：2026-09-23 05:35（Asia/Shanghai · UTC+08:00） · 来源：arXiv
- **一句话总结**：Crossflow 面向 Agentic LLM 服务，提出 Prefill-Decode 弹性方案。
- **项目亮点**：
  - P/D 分离通过阶段专门化与隔离提升服务效率
  - 现有收益建立在静态分区之上
  - 观察到阶段需求并非静态，需弹性调整
  - 面向 Agentic 场景的 serving 效率优化
- **关键字**：`Prefill-Decode Disaggregation`、`LLM Serving`、`Elasticity`、`Agentic`、`PD 分离`
- **相关链接**：
  - <https://arxiv.org/abs/2609.27085>

---

## 4. H-Spec

- **项目名称**：H-Spec
- **发表时间**：2026-09-21 15:11（Asia/Shanghai · UTC+08:00） · 来源：arXiv
- **一句话总结**：H-Spec 提出无需草稿侧 KV Cache 的并行投机解码方案。
- **项目亮点**：
  - 投机解码由草稿模型预测、目标模型验证，可无损加速
  - 块扩散草稿器并行预测多个 token，降低起草延迟
  - 方案取消草稿侧 KV Cache，减少显存开销
- **关键字**：`Speculative Decoding`、`KV Cache`、`Block Diffusion`、`Latency`、`Inference`、`Memory`
- **相关链接**：
  - <https://arxiv.org/abs/2609.24197>

---

## 5. SPLASH

- **项目名称**：SPLASH
- **发表时间**：2026-09-21 03:11（Asia/Shanghai · UTC+08:00） · 来源：arXiv
- **一句话总结**：SPLASH 协同设计稀疏注意力与高带宽 Flash，加速长上下文推理。
- **项目亮点**：
  - 上下文变长、并发提升，KV Cache 成为显存占用主因
  - HBM 带宽满足 decode 需求但容量有限
  - 片外内存与存储可提供容量补充
  - 稀疏注意力与高带宽 Flash 协同设计
- **关键字**：`KV Cache`、`Sparse Attention`、`High-Bandwidth Flash`、`Long-Context Inference`、`HBM`、`Decode`
- **相关链接**：
  - <https://arxiv.org/abs/2609.23816>

---

## 6. Hot-Cold Tiering

- **项目名称**：Hot-Cold Tiering
- **发表时间**：2026-09-22 15:18（Asia/Shanghai · UTC+08:00） · 来源：arXiv
- **一句话总结**：提出用 HBM 与 High Bandwidth Flash 做冷热分层，缓解 agentic LLM 服务中 KV 状态被驱逐的问题
- **项目亮点**：
  - 面向多轮 agent 会话，空闲期仍需保留完整上下文
  - GPU 显存容量有限，非活跃 KV 状态被迫驱逐
  - 恢复会话需重新计算或经互连传输，代价较高
  - 以 HBM 与 High Bandwidth Flash 构建冷热分层
- **关键字**：`HBM`、`High Bandwidth Flash`、`KV Cache`、`Agentic LLM Serving`、`Tiering`
- **相关链接**：
  - <https://arxiv.org/abs/2609.25782>

---

## 7. KV Cache Working Set

- **项目名称**：KV Cache Working Set
- **发表时间**：2026-09-23 19:59（Asia/Shanghai · UTC+08:00） · 来源：arXiv
- **一句话总结**：从 KV Cache 工作集视角提出 LLM 推理系统的在线容量规划方法
- **项目亮点**：
  - 前缀缓存复用已处理前缀的 KV 状态
  - 避免重复 prefill 计算，提升服务效率
  - 面向对话与工具调用历史持续增长的 agentic 负载
  - 以 KV Cache 工作集为核心做在线容量规划
- **关键字**：`KV Cache`、`Prefix Caching`、`Prefill`、`Capacity Planning`、`LLM Serving`
- **相关链接**：
  - <https://arxiv.org/abs/2609.27746>

---

## 8. KV-COBRA

- **项目名称**：KV-COBRA
- **发表时间**：2026-09-21 16:58（Asia/Shanghai · UTC+08:00） · 来源：arXiv
- **一句话总结**：提出联合优化秩与位宽分配的 KV Cache 压缩方法，面向极端低比特场景
- **项目亮点**：
  - 指出极端低比特下的瓶颈是预算分配而非压缩方案
  - 现有方法对各注意力头统一应用秩与位宽
  - 不同 head 最优的秩截断与量化组合并不相同
  - 联合优化秩截断与量化位宽的分配策略
- **关键字**：`KV Cache`、`Quantization`、`Rank Truncation`、`Attention Head`、`Compression`
- **相关链接**：
  - <https://arxiv.org/abs/2609.24298>

---

## 9. Disaggregated Quantization

- **项目名称**：Disaggregated Quantization
- **发表时间**：2026-09-22 20:44（Asia/Shanghai · UTC+08:00） · 来源：arXiv
- **一句话总结**：提出分离式量化，为 prefill 与 decode 阶段分别特化量化与存储策略
- **项目亮点**：
  - prefill 阶段用低精度算术加速 prompt 处理
  - decode 阶段用紧凑权重降低访存开销
  - 按阶段特化计算格式、权重与存储位置
- **关键字**：`Quantization`、`Prefill`、`Decode`、`Memory Traffic`、`LLM Inference`
- **相关链接**：
  - <https://arxiv.org/abs/2609.26333>

---

## 10. Power-Aware Provisioning

- **项目名称**：Power-Aware Provisioning
- **发表时间**：2026-09-21 22:17（Asia/Shanghai · UTC+08:00） · 来源：arXiv
- **一句话总结**：面向 prefill-decode 分离推理，提出兼顾服务容量与功耗的解析式资源规划方法
- **项目亮点**：
  - 功耗可用性日益约束 AI 推理集群的运行
  - 规划需同时考虑服务容量与功耗消耗
  - PD 分离已成为大规模推理服务的常见架构
- **关键字**：`PD Disaggregation`、`Prefill`、`Decode`、`Power-Aware Provisioning`、`Inference Serving`
- **相关链接**：
  - <https://arxiv.org/abs/2609.24639>

---

## 11. Amazon EKS

- **项目名称**：Amazon EKS
- **发表时间**：2026-09-26 00:29（Asia/Shanghai · UTC+08:00） · 来源：官方博客 RSS
- **一句话总结**：在 EKS 上结合 EFA 与 DeepEP 扩展 MoE 强化学习，聚合 rollout 吞吐提升 40%
- **项目亮点**：
  - EFA 提供低延迟、高带宽的节点间通信
  - DeepEP 承担 MoE 专家并行通信
  - 架构整合 Amazon EKS、EFA 与 Amazon S3
  - 面向大规模 RLHF 与 GRPO 训练场景
- **关键字**：`MoE`、`Reinforcement Learning`、`EFA`、`DeepEP`、`Amazon EKS`、`RLHF`
- **相关链接**：
  - <https://aws.amazon.com/blogs/machine-learning/scaling-moe-reinforcement-learning-on-amazon-eks-with-efa-and-deepep-with-40-more-throughput/>

---

## 12. NVIDIA

- **项目名称**：NVIDIA
- **发表时间**：2026-09-24 23:00（Asia/Shanghai · UTC+08:00） · 来源：官方博客 RSS
- **一句话总结**：介绍面向生物基础模型的 MoE 高效训练，替代成本高昂的稠密架构扩展
- **项目亮点**：
  - 指出稠密 Transformer 中每个 token 都经过所有层，扩展成本持续上升
  - 以 MoE 稀疏架构缓解稠密模型的扩展开销
  - 聚焦生物基础模型这一应用场景
- **关键字**：`MoE`、`Transformer`、`Foundation Model`、`Training Efficiency`、`Scaling`
- **相关链接**：
  - <https://developer.nvidia.com/blog/efficient-moe-training-for-biological-foundation-models/>

---

## 13. SkyRL

- **项目名称**：SkyRL
- **发表时间**：2026-09-26 00:18（Asia/Shanghai · UTC+08:00） · 来源：官方博客 RSS
- **一句话总结**：在 SageMaker HyperPod 上用 SkyRL 以 GRPO 后训练 Qwen3-VL-8B 视觉语言模型
- **项目亮点**：
  - 开源强化学习框架 SkyRL 运行于 SageMaker HyperPod
  - 以 GRPO 对 Qwen3-VL-8B 多模态模型做后训练
  - 流程覆盖容器镜像构建与 Ray 集群启动
  - 通过 SageMaker Studio 提交并监控训练任务
- **关键字**：`SkyRL`、`SageMaker HyperPod`、`GRPO`、`Multimodal RL`、`Ray`、`Qwen3-VL`
- **相关链接**：
  - <https://aws.amazon.com/blogs/machine-learning/accelerate-multimodal-rl-training-with-skyrl-on-amazon-sagemaker-hyperpod/>

---

## 14. Agentic CUDA Kernel Optimizer

- **项目名称**：Agentic CUDA Kernel Optimizer
- **发表时间**：2026-09-25 18:32（Asia/Shanghai · UTC+08:00） · 来源：Hacker News
- **一句话总结**：用 AI Agent 自动运行、基准测试并借 Nsight 剖析 CUDA 内核
- **项目亮点**：
  - 提供 C++ CUDA 测试脚手架供 AI Agent 调用
  - Agent 可运行内核并获取基准测试结果
  - 支持通过 Nsight 做性能剖析
  - 基于 LangGraph 搭建 Agent 流程
- **关键字**：`CUDA`、`Kernel Optimization`、`AI Agent`、`LangGraph`、`Nsight`、`Benchmark`
- **相关链接**：
  - <https://github.com/bertaye/agentic-cuda-optimizer>
  - <https://news.ycombinator.com/item?id=49842596>

---

## 15. vLLM

- **项目名称**：vLLM
- **发表时间**：2026-09-22 23:45（Asia/Shanghai · UTC+08:00） · 来源：官方博客 RSS
- **一句话总结**：vLLM 为硬件无关模型调整内部实现，放弃 fullgraph torch.compile 兼容
- **项目亮点**：
  - 为达到前沿性能改动 vLLM 内部实现
  - 改动使其与 fullgraph torch.compile 不再兼容
  - 可能影响依赖该编译模式的用户
  - 方向是支持硬件无关的模型
- **关键字**：`vLLM`、`torch.compile`、`Hardware-Agnostic`、`Inference`、`Compilation`
- **相关链接**：
  - <https://pytorch.org/blog/hardware-agnostic-models-in-vllm/>

---
