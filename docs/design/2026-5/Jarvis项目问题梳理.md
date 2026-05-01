1. ThreadManager 的锁粒度过大
2. sqlite共享有问题
3. checkpoint用的sqlite，好像不太行，考虑每个threadid一个单独的sqlite用来存储checkpoint
4. run_id设计有点不合理，多轮对话而言，一次启动应该是一个thread_id，然后每次用户请求都创建一个新的run_id，用来记录每次用户发起和jarvis的响应
5. 知识库功能工具化
6. contextualize节点的context summary并没有被利用上，应当完善，
- 让 _classify_intent 不再直接读 payload["instruction"]，而是读 state["context_summary"] 或 state["resolved_instruction"]
- 把 context_summary 从"展示字符串"变成"下游输入"
- 在 initial_state 里保留 previous_summary，让多轮对话的上下文能真正传递
7. classify_intent 改名为 understand_scene（场景理解，LLM 驱动）
      - 场景：code_task / deep_research / personal_knowledge / casual_chat / clarification
      - 复杂度：single_step / multi_step / open_ended
      - 风险预估：read_only / modify_fs / external_api

8. strategy节点对于错误处理全部到了blocked，这对于long run agent不可接受。
9. Coder skill 的默认超时是 1800 秒（app/config.py:44），但这里 WorkOrder 固定写死 30 秒。如果执行器不覆盖这个值，长任务会被强制中断。


## strategy
 1. 拆分职责：把"参数注入"抽到 WorkOrderBuilder 或 capability 自己的 preflight 方法里，strategize 只负责解析 planner 产出和调度任务。
  2. 消费上游意图：_candidate_capabilities_for_state 应该优先使用 state["allowed_tools"]（classify_intent 的产出），只在缺失时才兜底计算。
  3. 错误分层：LLM 超时 → retry；capability 不存在 → fallback 到 echo；eligibility 失败 → clarify 而非 blocked。
  4. 修复状态语义：clarification 和 last_error 分开存储；active_workers/worker_results 用 merge 而非 replace。
  5. TypedDict 补全：给 Task 补上 plan_step_id: str | None。

  总结

  strategize 是 Agent 的"中枢神经"，目前的实现能跑通基本流程，但在多轮恢复、错误韧性、职责边界上比较粗糙。如果 Jarvis 要支持深度研究、代码修改等复杂场景 ，这个节点需要重构——至少把参数注入和工具筛选逻辑解耦出去。


## worker order
worker order 这个概念似乎不必要，除非：
未来要扩展：
  - 把代码执行放到 Docker 容器/沙箱里
  - 把长任务发到消息队列
  - 需要跨进程序列化执行参数
决定：移除worker order
  - 

## task
"title": item.title or _title_from_tool_call(tool_name, tool_args, instruction),
  "description": item.description or instruction or tool.description,
  "dod": item.dod or f"{tool.name} completed successfully.",
  字段: title
  回退链: planner产出 → 从tool_call推断 → 原始instruction
  问题: 三个层级，优先 planner 的 title，但 planner 经常不填
  ────────────────────────────────────────
  字段: description
  回退链: planner产出 → 用户instruction → tool.description
  问题: 用户的"帮我改文件"和 tool 的"执行代码编辑"是完全不同的语义，混为一谈会让审计日志失真
  ────────────────────────────────────────
  字段: dod
  回退链: planner产出 → 模板字符串
  问题: 默认值的 {tool.name} completed successfully. 没有任何信息量，dod（Definition of Done）应该是可验收的具体标准，不是一句废话
  这三个字段的构造逻辑说明：Task 不知道自己该描述什么，它只是在收集各方碎片。
 动态字段 plan_step_id 破坏类型安全


tool_args 已被运行时污染

  在构造 Task 之前（:117-128）：

  if payload.get("workdir") and "workdir" not in tool_args:
      tool_args["workdir"] = payload["workdir"]
  if capability.name == "shell.command" and payload.get("command"):
      tool_args["command"] = payload["command"]

  tool_args 已经被注入了 workdir 和 command。这意味着：
  - Task 保存的不是"Planner 决定的参数"，而是"Planner 决定 + 运行时注入"的混合产物
  - 审计时无法还原"用户原始请求"和"系统注入"的边界
  - 如果同一个 Task 被重试，tool_args 可能已经包含过期上下文


class Task(TypedDict):
      id: str
      # --- 规划意图（只读，构造后不变）---
      intent: str           # 用户原始指令或 planner 的 step instruction
      tool_name: str
      tool_args: dict       # 干净的 planner 产出，不含运行时注入
      # --- 运行时上下文（执行前绑定）---
      workdir: str | None
      resource_key: str | None
      # --- 执行状态（流转）---
      status: TaskStatus
      created_at: str
      started_at: str | None
      completed_at: str | None
      # --- 结果（执行后填充）---
      result: SkillResult | None
      # --- 拓扑（可选）---
      dependencies: list[str]
      plan_step_id: str | None

  然后把 order_id 从 Task 里彻底移除。如果需要 WorkOrder 序列化，在 dispatch 节点做 task → WorkOrder 的映射，不要让 Task 背负外部系统的 ID。

  ---
  总结

  :158 的 Task 构造是一个应急式的设计：它试图同时满足 Planner 产出、WorkOrder 映射、状态流转、审计追溯四个需求，结果变成了一个字段语义混乱、类型不安全、 生命周期模糊的缝合体。如果要支持拓扑任务、多目录操作、精细化审计，这个 Task 模型必须重构。
 这对 Jarvis 的启示
  ┌────────────┬───────────────────────────────────────────┬───────────────────────────────┬───────────────────────────┐
  │   设计点   │                Jarvis 现状                │      Claude Code / Codex      │           结论            │
  ├────────────┼───────────────────────────────────────────┼───────────────────────────────┼───────────────────────────┤
  │ 任务结构   │ LangGraph 节点图 + Task 列表 + WorkOrder  │ 单代理 ReAct 循环，上下文驱动 │ Jarvis 过度设计了         │
  ├────────────┼───────────────────────────────────────────┼───────────────────────────────┼───────────────────────────┤
  │ 多步骤执行 │ work_plan + plan_step_id + dispatch_queue │ LLM 自己决定下一步调什么 tool │ 去掉拓扑结构，让 LLM 规划 │
  ├────────────┼───────────────────────────────────────────┼───────────────────────────────┼───────────────────────────┤
  │ 并行       │ 理论上支持（但实现不完整）                │ 只在工具层并行（读操作）      │ 无需 DAG，只需工具并发    │
  ├────────────┼───────────────────────────────────────────┼───────────────────────────────┼───────────────────────────┤
  │ 状态恢复   │ checkpoint + 业务 DB                      │ 追加事件日志 + Turn 挂起      │ 简化状态模型              │
  ├────────────┼───────────────────────────────────────────┼───────────────────────────────┼───────────────────────────┤
  │ 任务实体   │ Task + WorkOrder 双重结构                 │ 没有 Task 实体，只有事件 Item │ 可以大幅简化              │
  └────────────┴───────────────────────────────────────────┴───────────────────────────────┴───────────────────────────┘
  简单说：生产级代码助手没有"把请求拆成多个 Task 并调度执行"的概念。它们的做法是——给一个 Turn，LLM 自己通过 tool calling 把活干完。
  


## claude code的地位
claude code 现在被当做一次性执行的skill。而我们有两个场景，一个是用户让改代码，这时候应该是 
  下一个agent控制claude code，有问题抛出来。另外一个是deepresearch场景下让claude code写点统计数据的代码。

## clarify
人类澄清，是 Agent 的不确定性感知 + 人机协作节点，核心功能是对的——信息不足时问用户。但实现上比较粗糙：错误语义混乱、无限循环风险、不支持优雅拒绝、多轮澄 清体验差。在飞书群这种异步场景中，它的 UX 需要专门优化（比如@提问对象、设置超时自动取消）。
并且固定的clarify和自适性的jarvis是矛盾的，自适应的做法应该是 ReAct Loop:
    LLM 生成思考 → 如果需要更多信息 → 调用 ask_user("能否明确...") → 暂停
    用户回复 → 作为新 message 追加 → LLM 继续同一次循环
即有不懂的直接向用户提问，提问就是一次toolcall。和read_file同级。


## risk gate固定节点
risk_gate 作为固定图节点，假设"所有任务执行前都必须过一道安检门"。但自适应 Agent 的风险控制应该是分层的、场景化的：

  - 启动前：根据场景做一次性的权限授权（"允许改代码吗？"）
  - 运行时：危险操作由底层工具自己感知（Claude Code 问用户、Shell 命令由 OS 权限控制）
  - 审计层：所有操作事后记录，异常时告警
 固定 risk_gate 节点是事前审批模式，适合传统工作流，不适合长周期、多轮次、自探索的 Agent 场景。




dispatch (:313)

  做什么：把 dispatch_queue 里的 WorkOrder 逐个丢给 worker_client.dispatch()，然后标记 task 状态为 running。

  问题：

  1. 没有失败处理：client.dispatch(order)（:327）如果抛异常，整个图就崩了，不会进入 monitor，task 卡在 running 状态。
  2. 盲目 dispatch 所有：假设 dispatch_queue = [shell高风险, shell低风险]，risk_gate 已经放行了（可能因为同一个 thread 里已经审批过），但 dispatch 全部一气发出去，没有考虑依赖关系。
  3. dispatch_queue 清空后就丢了原始计划：:338 清空 dispatch_queue，如果后续需要重试或 replan，原始调度意图消失。
  4. 只支持 polling，不支持回调/事件：active_workers 是一个 {task_id: order_id} 字典，monitor 节点只能轮询，无法实现 worker 完成后主动通知。

  ---
  monitor (:346)

  做什么：轮询所有 active_workers，检查是否完成。如果都完成了，进入 aggregate；否则 interrupt 挂起等 resume。

  问题：

  1. 轮询 + interrupt 的奇怪组合：它先 poll 一次（:353），如果还有没完成的，立刻 interrupt（:360）。这意味着每次进入 monitor 最多只 poll 一次，然后就挂起
如果 resume 来自外部 worker 事件（如 worker_complete），它 resume 后再 poll 一次——本质上是用 LangGraph 的 interrupt 做异步事件等待，但 worker 完成事件   和审批/澄清的 resume 走的是同一个机制，非常混乱。
  2. _apply_worker_resume_events 的 hack：:366 这里处理 resume 时如果是 worker 完成事件，直接把结果写进 worker_results。但正常的 resume 应该是用户审批/澄，这里是把worker 完成也伪装成 resume 事件。这是把 worker 异步回调硬塞进 LangGraph 的 human-in-the-loop 机制里。
  3. 无法支持真正的并发：如果 active_workers 有 3 个，其中 2 个完成了，还剩 1 个在跑。此时 monitor 挂起。如果那个未完成的 worker 完成了，resume 后进入 aggregate。但如果用户此时发了新消息（飞书群里的正常对话），这个 resume 会被当成 worker 事件处理还是用户消息？事件路由不清晰。
  4. Claude Code 交互模式完全不可用：如果 worker 是交互式 Claude Code，它可能运行 10 分钟且中间暂停问用户。monitor 的轮询间隔是 0（进入节点 poll 一次就挂），既看不到实时输出，也无法把 Claude Code 的内部确认转发给用户。

  ---
  aggregate (:386)

  做什么：收集所有 worker 结果，更新 task 状态（success/failed/retry），如果有重试则生成新的 dispatch_queue 回 risk_gate。

  问题：

  1. 和 verify 的职责严重重叠：aggregate 处理 result → 更新 status → 决定 retry 或 failed。然后 verify 节点（:461）又做了一遍几乎相同的事（也处理 result 、retry、replan）。两个节点都在做"结果判断"。
  2. aggregate 和 verify 的状态跃迁不合理：
    - aggregate 里成功的 task 被标为 "verifying"（:433）
    - 然后进入 verify 节点，它只处理 status == "verifying" 的 task
    - 这意味着一个成功的 task 必须先经过 aggregate 标为 verifying，再到 verify 里重新判断一次是否真的成功

  这完全是多余的。aggregate 应该叫 collect_results，verify 应该叫 validate_results，但当前分工让它们互相纠缠。
  3. 重试时回到 risk_gate：:438 重试的 dispatch_queue 回到 risk_gate。一个已经审批过一次的 shell 命令，因为超时重试，又要重新走审批？这在生产环境会让用户掉。
  4. work_plan 状态维护混乱：aggregate 不更新 work_plan，verify 才更新。这导致 aggregate 阶段看不到 plan step 的进度。

  ---
  verify (:461)

  做什么：对 aggregate 标为 verifying 的 task 做最终验收，LLM 语义判断是 success/retry/replan/failed/blocked。

  问题：

  1. 和 aggregate 的职责重复：前面说过了，两个节点都在处理 worker result、retry、failed 逻辑。
  2. 语义验收的触发条件奇怪（verification.py:48-93）：

  def is_objective_success(task: Task) -> bool:
      if task.get("verification_cmd"): return True
      if tool_name in {"shell.command", "shell.test", "answer.echo"}: return True
      if worker_type != "coder": return True
      return False

    - 有 verification_cmd → 直接认为成功（不运行 verification_cmd，也不让 LLM 判断，直接 success）
    - shell、echo 工具 → 直接 success
    - 只有 coder 才进入 LLM 语义验收

  这意味着 Deep Research 的搜索 task 永远不会被 LLM 验证，它只会因为 result.ok=True 就被 aggregate 标为 verifying，然后 verify 节点看到 is_objective_success=True 直接 success。搜索结果的"质量"完全没被评估。
  3. replan 逻辑在 verify 层处理（:501）：

  elif assessment.decision == "replan":
      updated["status"] = "cancelled"
      needs_replan = True

  3. 如果 LLM 认为当前计划行不通，需要 replan。这个决策在执行完成后才做，而不是在规划阶段。正确的做法是规划时就预判，而不是等执行失败后再绕一圈。        
  4. work_plan 完成状态的 race condition：:530-544

  if work_plan and _next_pending_plan_step(work_plan) and not failed:
      return _goto("strategize", ...)
  if work_plan and not _next_pending_plan_step(work_plan) and not failed:
      work_plan["status"] = "completed"

  4. 这段逻辑判断是否还有未完成的 plan step。但 work_plan 的更新分散在 verify（:486 标记 step 完成）和 strategize（:739 取 next step）里，维护非常困难。 
  5. needs_replan 时清空 worker_results（:555）：

  "worker_results": {},

  5. replan 后之前的 worker 结果全部丢失。如果 replan 只是调整后续步骤，之前已经成功完成的 task 结果应该保留作为上下文。

  ---
  wait_approval (:570)

  做什么：interrupt 挂起等用户审批，批准后把 task 加入 dispatch_queue，回到 risk_gate。

  问题：

  1. 只能审批一个 task：如果 risk_gate 里多个 task 需要审批，它只拦截第一个（:288 return）。用户一次只能审一个，效率极低。
  2. 审批后重新走 risk_gate（:614）：

  return _goto("risk_gate", ...)

  2. 审批通过的操作又回去过一遍 risk_gate，虽然 approved_order_ids 能命中，但这是无意义的绕路。
  3. pending_action 和 WorkOrder 的重复构造：:589-601

  3. 审批后如果 order_dump 找不到，现场用 pending_action 的字段构造一个新的 WorkOrder。这意味着 risk_gate 里构造的 WorkOrder 和 wait_approval 里构造的可 能不一致。
  4. 用户拒绝后全部 blocked：:626

  tasks = _update_current_task(state, status="blocked", result_summary="Rejected by user.")
  return _goto("blocked", ...)

  4. 用户拒绝一个高风险操作，整个 thread 进入 blocked。如果后面还有其他低风险的 task 可以执行，它们也一起死了。
  5. 和 Claude Code 交互模式的冲突：如果 Claude Code 是交互式运行，它内部也有"确认"机制。wait_approval 是 Jarvis 层的审批，Claude Code 有它自己的审批，两叠加。

  ---
  summarize (:640)

  做什么：生成最终回复给用户。

  问题：

  1. LLM 总结失败后的 fallback 太粗糙：如果 _synthesize_final_answer 返回 None，它直接统计成功/失败数量拼接字符串（:646-652）。对于 Deep Research 或 Claude Code 改代码的场景，这种统计毫无意义——用户要看的是研究报告或变更摘要，不是"Completed 3 task(s); 1 failed"。
  2. failed == 0 时标 completed，否则标 failed：:653

  2. 如果 4 个 task 里 3 个成功、1 个失败，整个 run 标 failed。但在多步骤研究里，某一步搜索失败可能不影响整体结论，不应该一刀切标失败。
  3. 不看 work_plan 的产出：summarize 只消费 task_list，如果任务是一个复杂的研究计划，plan 的结构信息（哪些 step 完成了、结论是什么）完全没被用来组织总结。

  ---
  blocked (:656)

  做什么：终点节点，把状态标为 blocked，更新当前 task。

  问题：

  1. 是所有错误的垃圾桶：前面任何节点出问题（strategize 失败、dispatch 异常、aggregate 里 result missing、verify 里 assessment blocked、wait_approval 被 拒绝）都进入 blocked。
  2. last_error 语义混乱：:662
  result_summary = state.get("last_error") or state.get("final_summary") or "Task blocked."
  2. last_error 被用来存 clarification question（前面讨论过），也被用来存 planner 失败、worker 失败、用户拒绝等完全不同的错误。blocked 节点无法区分"LLM  超时"（可重试）、"用户拒绝"（不可逆）、"代码执行错误"（需要调试）。
  3. 没有恢复路径：进入 blocked 后图结束（graph.add_edge("blocked", END)）。如果只是一个 task 失败，其他 task 成功了，用户想继续执行剩余 task，没有办法。

  ---
  总结
  ┌───────────────┬──────────────────────────────────────────────────┐
  │     节点      │                     核心问题                     │
  ├───────────────┼──────────────────────────────────────────────────┤
  │ dispatch      │ 无失败处理，无依赖感知，只能轮询                 │
  ├───────────────┼──────────────────────────────────────────────────┤
  │ monitor       │ 轮询+interrupt 的组合是 hack，不支持实时流式输出 │
  ├───────────────┼──────────────────────────────────────────────────┤
  │ aggregate     │ 和 verify 职责重叠，重试回到 risk_gate           │
  ├───────────────┼──────────────────────────────────────────────────┤
  │ verify        │ 验收逻辑偏袒 coder/歧视 search，replan 清空结果  │
  ├───────────────┼──────────────────────────────────────────────────┤
  │ wait_approval │ 单 task 审批，拒绝即全死，构造 WorkOrder 重复    │
  ├───────────────┼──────────────────────────────────────────────────┤
  │ summarize     │ fallback 粗糙，只看 task 数量不看 plan 结构      │
  ├───────────────┼──────────────────────────────────────────────────┤
  │ blocked       │ 所有错误一锅炖，无恢复路径                       │
  └───────────────┴──────────────────────────────────────────────────┘
  根本问题还是同一个：这套图是为"工作流调度"设计的，假设每个请求都能预先拆成 task 列表，然后调度执行。但你的 Jarvis 需要的是对话驱动、自适应、长周期的运 行时。这些节点里的 poll/interrupt/retry/risk_gate 机制，在 Claude Code 交互和 Deep Research 自适应探索的场景下，全是阻碍而非帮助。
