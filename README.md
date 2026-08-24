# astrbot_plugin_chat_ratelimit

AstrBot 群聊 LLM 对话总量限流插件。当**所有用户合计**的对话频率达到配置的限额后，再 @Bot 不会触发 LLM 对话（不消耗 token），Bot 会随机回复一条你自定义的提示语。

## 功能

- ✅ 全群共享配额：按"所有用户合计"计数，不是按单人计数
- ✅ 四档滑动窗口限额，均可独立配置，`0` 表示不限制：
  - 每分钟
  - 每半小时
  - 每小时
  - 每天
- ✅ 超限后随机回复提示语（在插件管理页配置，支持多句随机抽取）
- ✅ 可选是否同样限制私聊（默认只限制群聊）
- ✅ 管理员命令 `/ratelimit` 查看当前各窗口用量
- ✅ 纯 Python 标准库实现，无第三方依赖，Windows / Linux 通用

## 安装

方式一（推荐）：AstrBot 管理面板 → 插件市场 → 从仓库安装，填入：

```
https://github.com/xiaohao234/astrbot_plugin_chat_ratelimit
```

方式二：将本仓库克隆/下载后，把 `astrbot_plugin_chat_ratelimit` 文件夹放到 AstrBot 的 `data/plugins/` 目录下，重启 AstrBot 或在插件管理页重载。

## 配置说明

安装后进入 AstrBot 管理面板 → 插件管理 → 本插件 → 配置：

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| enabled | 开关 | true | 总开关 |
| limit_per_minute | 整数 | 5 | 每分钟最多 LLM 对话次数（合计，0 = 不限制） |
| limit_per_half_hour | 整数 | 30 | 每半小时最多次数（0 = 不限制） |
| limit_per_hour | 整数 | 0 | 每小时最多次数（0 = 不限制） |
| limit_per_day | 整数 | 200 | 每天最多次数（0 = 不限制） |
| reply_texts | 列表 | 内置 3 句 | 超限后随机回复的句子，每句一行 |
| also_limit_private | 开关 | false | 是否同样限制私聊 |

## 工作原理

插件以高优先级监听群消息。当消息唤醒了 Bot（被 @）时：

1. 检查四个滑动窗口内的对话次数是否超出限额
2. 未超限 → 正常放行，触发 LLM，并在所有窗口计数 +1
3. 已超限 → 随机回复一条 `reply_texts` 中的提示语，并终止事件传播（LLM 完全不会被调用）

## 已知限制

- 计数保存在内存中，AstrBot 重启后各窗口计数清零（重启后即恢复可用）。
- 仅统计"唤醒 Bot 的消息"，即真正会触发 LLM 的对话。

## Author

[xiaohao234](https://github.com/xiaohao234)
