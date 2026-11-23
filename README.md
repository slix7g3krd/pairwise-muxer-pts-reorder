# pairwise-muxer-pts-reorder

## 领域

游戏、图形与多媒体

## 主题

封装时间戳重排

## 说明

B 帧 DTS、timebase 换算与索引写入时序。

初始状态下 `media/muxer_pts_reorder.py` 中所有函数抛出 `NotImplementedError`，测试全部失败。
修复实现后运行：

```bash
python -m pytest -q
```

期望全部通过。
