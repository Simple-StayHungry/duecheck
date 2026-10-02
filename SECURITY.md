# Security

公开仓库不得包含真实业务输入输出、运行数据库、日志、缓存、个人绝对路径或凭据。发布前执行：

```bash
python tools/public_release_check.py
```

如果敏感文件曾经进入 Git 提交历史，仅在新提交中删除并不足够；应重写历史或重新建立干净仓库。
