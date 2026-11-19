"""hai-omni-gateway —— vllm-omni 音频栈的统一入口网关。

定位：对外提供 OpenAI 兼容接口，对内负责
  1) 路由：按请求里的 model 名路由到对应 vllm-omni 实例
  2) 调度：一张卡同一时刻只让一个模型醒着，按需 sleep / wakeup + resume
  3) 队列：换台期间请求排队（而不是挂死），并做背压
"""

__version__ = "0.1.0"
