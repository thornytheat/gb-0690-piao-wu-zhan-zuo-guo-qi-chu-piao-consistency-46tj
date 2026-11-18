# 运行维护

主服务端口与Compose保持一致。数据保存在records命名卷，应用代码不需要写入权限。

```sh
docker compose logs --tail=80 app
docker compose restart app
docker compose exec -T app pytest -q
docker compose down --remove-orphans
```

健康检查GET /health。失败输入返回原因码，结构性错误422，过大文件413，陈旧复核409。
文件内容最大4MB。批次先计算再原子保存，失败不留半条记录。证据CSV按原始行号定位。

## 团购补位操作

M8 页面可查看订单完成状态、待补位计划与原票/补位凭证，也可调用 `POST /api/backfill/<run_id>`（body：`{"version":整数,"seats":[...]}`，seats 省略即自动分配）。版本不符返回409，旧版状态保留；补位成功版本加一，重跑无缺失座位时 `added=0` 且版本不变。核对导出 `GET /api/backfill.csv?run=<id>` 分原票、补位、订单三段。补位记录与批次同库存放，删除 records 卷前确认不再需要。
