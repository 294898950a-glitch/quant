# Google 烧掉 $190B 不是在造未来，是在修墙

所有人都说 Google 在 AI 上疯狂砸钱是为了抢占下一个时代。Pichai 宣布 2026 年 CapEx $180-190B 的时候，媒体标题清一色是"AI 军备竞赛"。CapEx 同比暴涨 107%，Q1 就花了 $35.67B。

但如果你问一个更冷的问题：这些钱花下去，有多少是在创造新收入，有多少只是防止旧收入消失？

你会发现大部分是后者。

Google 的 $200B+ 搜索广告业务正在被 AI 直接威胁——用户不再输入关键词，而是对 AI 说一句话。如果 Google 不能在自己的搜索结果里嵌入同样好甚至更好的 AI 回答，用户就会走。所以 Google 必须建。不建就是等死。但建了也不意味着比原来赚更多——只是回到了"没被抢走"的起点。

这叫防御性 CapEx。回报不是"赚更多"，而是"没变少"。

## 那个看起来合理的叙事

绝大多数人看 Google CapEx 的逻辑是这样的：AI 是下一个万亿美元市场，Google 有 TPU 自研芯片、有 Search 入口、有 Cloud 基础设施，现在大举投入，未来就能在 AI 时代继续当霸主。CapEx 暴涨只是一个过渡期，ROIC 暂时下降，等 AI 收入起来就会恢复。

这个叙事有一个致命的漏洞：它假设 Google 的 CapEx 主要是在"进攻"——造新的收入引擎。

但实际上，Google 的 CapEx 结构指向另一个方向。

## 地基是 Search，不是 Cloud

Alphabet 2026 年 Q1 的收入结构里，Google Search 贡献了 $60.4B，占 Alphabet 总收入的绝大部分。Search 本身就需要 AI 基础设施——AI Overviews、AI Mode、多模态搜索——这些不是附加功能，就是 Search 本身在 AI 时代的形态。

这意味着 Google 的 TPU 和数据中心，首先是一个"维持 Search 竞争力"的必需品，其次才是一个"对外销售 Cloud 算力"的增量生意。

Pichai 在 Q1 电话会议上亲口说了："severe computing constraints"——严重的算力约束。算力不够，不是因为 Cloud 客户买太多，而是 Google 自己就不够用。（TechCrunch, 2026-04-29；富途牛牛，2026-04-29 交叉验证）

Google Cloud 的 backlog 确实惊人——$462B，年化 run rate $80B，增速 63%。但这笔 backlog 是在算力已经"严重约束"的情况下签的。Google 连自己的 Search 都喂不饱，Cloud 客户还在排队。

这说明什么？说明现在的 CapEx 不是在为 Cloud 扩张买单——Cloud 客户的钱 Google 想赚但产能跟不上。CapEx 的优先方向是解自己的渴。

## 43% 这个数字在科技行业没有先例

CapEx/收入比 43%，这在科技行业没有先例。

Amazon 转型 AWS 时峰值约 12%。Microsoft 推 Azure 时约 18%。TSMC 的资本密集度更高，但 TSMC 生来就是重资产公司——这是基因，不是转型。（Wikipedia Alphabet Finances，历史对照）

Google 从 2016 年的约 11% 爬到 2026 年的约 43%，是在不到十年内完成的。用投资术语说：一家轻资产广告公司在变成一家重资产基础设施公司。

这不是好或坏的判断。这是一个事实——而大多数投资者还没把资产性质的改变纳入估值模型。

## ROIC 在诚实下降

Google 的 ROIC 从 2024 年的 35.4% 降到 2025 年的 29.4%，降了 600 个基点。降幅是三家超大规模云厂商里最大的——Microsoft 只降了 1.1 个百分点，Amazon 降了 3.8 个百分点。（Macrotrends，自算；StockAnalysis 交叉验证 ROIC=28.34%, WACC=11.01%）

但重要的不是下降本身——投入资本膨胀 37.7% 而 NOPAT 只增长 14.3%，ROIC 下降是数学必然。重要的是：这个下降是否可逆？

Pichai 用的词是"considers ROIC"，不是"requires ROIC"——ROIC 是方向校准，不是硬性门槛。（TechCrunch, 2026-04-29；autogpt.net 双源验证）

在同一句话里，他说"takes an approach that considers ROIC"。考虑。不是要求。

当 CapEx/收入比只有 11% 的时候，"考虑"就够了——因为投入小，容错空间大。当 CapEx/收入比到 43% 的时候，"考虑"这个问题本身就需要被重新考虑。

## Microsoft 的问题是"需求会不会走"

做一个对比会让情况更清楚。

Microsoft 的 Azure AI 推理收入约 $37B 年化，其中 OpenAI 占了约 31%——约 $11.5B。（TechCrunch, 2025-11-14，Ed Zitron 获取的泄露文件：2024 年 $3.8B，2025 年前 9 个月 $8.65B）

更扎眼的是 backlog 集中度：OpenAI 驱动了 Azure 商业合同积压的 45%。（Business Insider；The Register；the-decoder.com 三源交叉验证）

这意味着 Microsoft 的 Azure AI 增长有两个前提：OpenAI 继续增长，且 OpenAI 继续把算力需求放在 Azure 上。

这两个前提在 2026 年 4 月同时松动了。

Wikipedia OpenAI 词条记录了这次重组：Microsoft 不再是 OpenAI 的 exclusive cloud provider——只保留了"first right to provide"。AGI 条款被删除。OpenAI 可以在 Microsoft 产能不足时向其他供应商采购。

本质上，Microsoft 用"独家"换了"确定"——OpenAI 承诺了 $250B 的 Azure 购买。但"独家"和"确定"在长期是两回事。OpenAI 同时在自建 Stargate、签约 Oracle $300B、使用 Google Cloud TPU、与 Broadcom 合作自研芯片。

Microsoft 的 CapEx 回报，压在了一个正在建立备选项的客户身上。

## Amazon 的差异化最弱

Amazon 的 AWS 是市场份额第一，Trainium 芯片年化 run rate 到了 $20B，声称比 NVIDIA 方案节省 50% 成本。（Let's Data Science, 2026-04-29；24/7 Wall St, 2026-02-28）

但 Trainium 是一个后来者。它的差异化——更便宜——如果 NVIDIA 降价或者 Google TPU 证明更高效，这个差异化就会缩水。而且 Amazon 的内部 AI 需求（Alexa、零售）远小于 Google 的 Search AI 和 Microsoft 的 Copilot——内部需求底座不够厚。

三家都在烧钱。烧钱的方式不同。

## 烧钱有三种结局

用第 10 轮追问里搭建的三情景框架来看：

**真需求**：如果 Search AI 和 Cloud 的外部需求都是真实的、非泡沫的，那 CapEx 最终会转化为高回报——ROIC 可能恢复。

**混合消化**：如果 Cloud 外部需求部分有水分（比如 $462B backlog 里含有循环融资或期权型意向合同），但 Search 内部可以消化 Cloud 剩余的产能。ROIC 不会回到 35%+，但资产不会闲置——复利机制受损但不毁。

**泡沫破裂**：如果需求被严重高估，算力大面积闲置，资产折旧吞噬利润。这种情况在电信行业发生过——1996 年后五年，电信设备投资超 $500B，大部分债务融资，产能增长远超需求，债券投资者最终只回收了 20%。（Wikipedia）

Google 的结构性优势是：自营现金流而非债务融资，且有 $200B+ 的 Search 内部需求底座。这大幅降低了第三种结局的概率。但它不能消除。

## CapEx 不能拆开来看——而这正是问题

Google 的基础设施是"双用途"的：同一批 TPU 和数据中心，既服务 Google 内部的 Search/Gemini，也服务 Cloud 外部客户。物理上不可拆分，会计上也不拆分。

这套架构是 Google 从 2003 年就确立的原则——当年那篇著名的 IEEE 论文里，Google 论证了用廉价硬件集群做可靠基础设施的可行性。TPU 是这个基因在 AI 时代的延续。（Wikipedia，2003 年 IEEE 论文）

但"物理不可拆分"意味着"回报不可独立评估"。外部投资者无法判断：Cloud 的 $462B backlog 多久能转化为利润？Search AI 到底消耗了多少内部算力？CapEx 是否超过了 Search AI 的实际需要？

Pichai 承认"cloud revenue would have been higher if we were able to meet that demand"。（TechCrunch, 2026-04-29）

这句话翻译过来：Cloud 客户愿意付钱，但我们没货。没货的原因，一部分是产能不足，一部分是内部也在抢。

## 谁在守门？

Ruth Porat 自 2023 年 9 月起担任 Alphabet 总裁兼 CIO，职责明确包括"infrastructure and data centers"投资监督。（Wikipedia Ruth Porat 词条；SiliconANGLE, 2023-07-25）

Alphabet 的结构受 Berkshire Hathaway 启发——Schmidt 在 2017 年鼓励 Page 和 Brin 去 Omaha 见 Buffett。（Wikipedia Alphabet Inc. § History）模式是分散经营、集中资本配置。Porat 就是那个集中的守门人。

$185B 一年的 CapEx 纪律，实质押在一个人身上，而非制度化的多人制衡流程。

公开资料中找不到 Alphabet 有类似 Amazon 单线程领导（STL）的 CapEx 治理机制。产品层面 Google 有关停文化——Fiber 2016 年暂停、Stadia 2023 年关停并退款、Google+ 2019 年关闭——但在数据中心和 TPU 这种基础设施层面，未见公开纠偏案例。（Wikipedia 各词条）

Logistics Viewpoints 2026 年 1 月有一个精准的警告："component monoculture locks in risk years before it becomes visible"——组件单一文化把风险锁进去了，等看见的时候已经晚了几年。

## 回到那个问题：Search AI 到底需要多少算力

这不是一个财务问题。这是一个工程问题。

如果 Google 今天的 CapEx 里只有 50% 是 Search AI 真正需要的，剩下的 50% 是 Cloud 增长和"先占住再说"的期权型投入——那"需求地板"的保护力就被高估了。剩余的 50% 还是依赖外部客户。

如果 Search AI 本身就吃掉了当前 CapEx 的大头，那 Microsoft 面临的"需求可能走"就比 Google 面临的"CapEx 过剩"更危险。

这个问题暂时没有答案。Google 不披露这个拆分，可能也无法拆分。

但我们至少可以确定一件事：用"需求地板"论证 Google 的 CapEx 安全，在逻辑上成立，在地板的水平线上不确定。地板存在。地板有多高，不知道。

## 这篇文章让长期 thesis 更强、更弱，还是只是更清楚

更清楚，但方向偏弱。

Google 的护城河从"单一变复合"的主 thesis 没有被动摇——TPU 全栈整合、Search 入口控制、双用途基础设施的灵活性，这些都是真实的差异化优势，Meta 和 Anthropic 签约 TPU 是用脚投票。

但"防御性 CapEx 天然 ROIC 低于进攻性 CapEx"这个现实，让 thesis 里隐含的"护城河会带来高回报"的假设被削弱了。护城河可能仍然在，但护城河后面可能不是高利润率的城堡，而是一座维护成本越来越贵的要塞。

Google ROIC 可能长期稳定在 22-24%，而非恢复 35%+。如果接受这个判断，Google 的投资价值需要从"高 ROIC 复利机器"重新定义为"稳定但不特别高的 ROIC + 极低的毁灭概率"——这更接近一家公用事业公司的剖面，而不是一个科技平台。

## 下一步最应该追问什么

从工程角度估算 Search AI 的实际算力需求。不是财务数字，是推理成本、索引规模、SRE 容量规划方法论层面的拆解。需要 Google SRE Book、公开的 TPU 性能数据、AI 推理成本基准这些材料。如果这个拆解的结果是 Search AI 只需要当前 CapEx 的 40-60%，那 thesis 需要进一步向下修正。

## 哪个关键判断仍需查证

CapEx 的季节性模式。Q1 的 CapEx $35.67B 被直接用于计算"78% 经营现金流被吞噬"，但 CapEx 有前置特征——Q1 往往启动新项目多、付款集中。如果全年 CapEx 不是均匀分布在四个季度，年度 FCF 被吞噬的比例可能低于 Q1 单季显示的 78%。需要复查 Alphabet 10-Q 中 CapEx 的季节性历史模式。

---

## 本文依据的材料

- topic: google
- source output: batch_id 2026-05-04_google，第 6-14 轮追问原始研究材料
- thesis: Google 的护城河从"单一变复合"——Search 入口 + TPU 全栈 + 双用途基础设施形成 AI 时代的差异化壁垒，但 CapEx 的防御性特征意味着 ROIC 可能长期停留在 22-24% 而非恢复 35%+
- key anchors:
  - Search 广告有机增长 31%（Search Engine Journal, 2026-05-02）
  - CapEx 同比暴涨 107% 至 $35.67B Q1，全年指引 $180-190B（24/7 Wall St, 2026-04-30；富途牛牛, 2026-04-29）
  - CapEx/收入比从 11% 升至 ~43%（Wikipedia Alphabet Finances 历史对照）
  - ROIC 从 35.4% 降至 29.4%（Macrotrends 自算；StockAnalysis 交叉验证）
  - Cloud backlog $462B，run rate $80B（CRN/VentureBeat 多源验证）
  - OpenAI 占 Azure AI 推理收入 ~31%，占 backlog 45%（TechCrunch 泄露文件；Business Insider 多源验证）
  - Microsoft 2026 年 4 月失去 Azure 独家供应商地位（Wikipedia OpenAI 词条）
  - Google 基础设施"双用途"架构源自 2003 年 IEEE 论文（Wikipedia）
  - Ruth Porat 为 CIO 守门人，Alphabet 结构受 Berkshire Hathaway 启发（Wikipedia 各词条）
  - 产品层面有关停文化但基建层面无公开纠偏案例（Wikipedia Google Fiber/Stadia/Google+ 词条）
