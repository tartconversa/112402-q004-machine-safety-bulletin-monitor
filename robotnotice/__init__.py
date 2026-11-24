"""设备安全通告采集与适用批次处置系统。

模块划分：
- versions: 型号别名归一化、序列号字符串闭区间、固件通配匹配
- evidence: 只追加的内容寻址原始证据库
- storage: SQLite 持久层与审计事件
- collector: 公告页面/接口轮询、解析与差异识别
- assets: 台账导入、别名解析与待核项
- matching: 适用范围评估
- workflow: 风险事项、工程师决策、调度、检修与解除
- console/ cli: 查询控制台与命令行入口
"""

__version__ = "1.0.0"
