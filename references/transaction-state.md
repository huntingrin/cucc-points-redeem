# 本地计划、提交断点和结果

`scripts/transaction_state.py`仅处理本地JSON；Python 3标准库，文件锁使用macOS/Linux的`fcntl`。没有Python/该模块时由Agent维护同样的私有断点，不能因此跳过恢复核对。

为每笔已选定兑换使用一个新的任务私有文件。不要放在技能安装目录。脚本不会删除或重置已有交易。

浏览器任务空间、页面标签、当前交接状态和精确下一步由Agent另存同目录的任务上下文，关联本笔状态文件。该脚本不维护浏览器所有权；恢复时同时读取任务上下文和交易状态，再按宿主规则接回现有页面。

## 输入

已授权计划示例（示例号码为虚构脱敏标识，商品与价格必须替换为本轮实况）：

```json
{
  "account": "139****0000",
  "category": "data",
  "product_id": "本轮官网取得的ID",
  "product_name": "本轮商品完整名称",
  "contents": ["本轮商品及全部附带权益"],
  "quantity": 1,
  "points": 300,
  "cash": "0.00",
  "validity": "本轮已核有效期和到期日",
  "renewal": "none",
  "authorization_confirmed": true
}
```

类别使用`data`、`voice`或`media`。只有确有本笔交易授权时才能设置`authorization_confirmed: true`。现金必须为0，积分和数量为正整数；有效期、完整组合及无续订应从当前规则核实。

提交观察JSON包含计划中除`authorization_confirmed`外的相同字段，并补充：

```json
{
  "stage": "checkout",
  "loaded": true,
  "applicable_points": 1000,
  "verification_required": true,
  "verification_completed": true
}
```

观察来自本次真实确认订单和与该商品身份一致的已读详情，不能直接复制计划当作观察。`verification_completed`只记录本人已在页面完成且页面可继续，不读验证码内容。所选积分适用及余额判断由Agent按商品规则完成，脚本只检查已提取的可用额度。

## 命令

```bash
python3 /absolute/skill/scripts/transaction_state.py --state /absolute/task/transaction.json prepare --plan /absolute/task/plan.json
python3 /absolute/skill/scripts/transaction_state.py --state /absolute/task/transaction.json arm-submit --observed /absolute/task/observed.json
# arm-submit返回submit_allowed后，立即在同一页面执行一次最终提交。
python3 /absolute/skill/scripts/transaction_state.py --state /absolute/task/transaction.json record --result /absolute/task/result.json
python3 /absolute/skill/scripts/transaction_state.py --state /absolute/task/transaction.json status
```

`arm-submit`会先比对账户、类别、商品ID/名称、完整内容、数量、积分、现金、有效期与续订，再检查业务数据和验证是否完成；最后原子写入`submitting`，才返回`submit_allowed`。它不会点击网页。再次调用不能再次取得提交许可。

如果当前页面已经成功、处理中或结果不明，先`record`或只读对账，不能用新文件绕开本笔记录重提。已写`submitting`后即使浏览器点击失败也先对账；无订单号且结果不明时记录`needs_reconcile`。

## 记录结果

```json
{
  "phase": "order_completed",
  "account": "139****0000",
  "product_name": "与计划一致的完整商品名称",
  "order_id": "官网订单号",
  "points_spent": 300,
  "cash_paid": "0.00",
  "communication_balance": 700,
  "reward_balance": 0,
  "source": "官方订单详情及实际余额查询"
}
```

`phase`支持`awaiting_verification`、`paid`、`processing`、`order_completed`、`benefit_verified`和`needs_reconcile`。成功页尚无订单号时可记录`paid`并继续查订单；`processing`及完成状态需订单号；`order_completed`和`benefit_verified`还需匹配账户、商品名。

独立取得运营商回执/权益明细后才记录`benefit_verified`，并提供`benefit_source`，如“本次10010流量开通成功回执”；平台订单完成本身不够。结果字段未知就省略，不按预计成本补齐实际扣分或余额。

同一记录不接受替换订单号。实际扣分/现金与计划不符时保存观察并进入`needs_reconcile`。旧的较低进度观察不会把已完成交易降回可提交状态。所有提交或结果阶段都禁止再次取得提交许可。

脚本只接受文档中的字段，拒绝额外输入字段；不要向JSON写入密码、验证码、Cookie、短信历史或浏览器会话凭据。状态与锁文件权限为600。此机制只保护单个本地记录，不能提供网站未支持的“恰好一次”保证。
