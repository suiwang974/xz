DS娘(大肥鱼) Mao 结构修复包 —— 安装说明
================================================
来源: 作者 Kamiya_Ryoko 在 VGen 的免费领取包, 本包只是把同一批文件按 Mao 桌宠结构重排 + 修了两个断链,
没有新增模型文件, 也没有 .cmo3 可编辑源文件(作者本来就没公开, 这是正常的, 不是缺件)。

原包两个实测问题(已在本包修好):
1. 原 大肥鱼.model3.json 里完全没有 Motions / Expressions 引用, Animation/ 里 8 个动作、
   Exp3/ 里 23 个表情只是散文件, 按 Cubism 规范加载的程序认不到它们, 看起来就像"不完整"。
   本包已把 8 个动作和 23 个表情写进 model3.json; 原文件备份为 大肥鱼.model3.original.json。
   注意: 主聊之前说的"25 个表情"是数错了, 实测 Exp3/ 里是 23 个。
2. 原 大肥鱼.vtube.json 的待机动画写的是根目录 IDLE.motion3.json / 脱捕.motion3.json,
   但这两个文件实际在 Animation/ 里, 引用是悬空的; 本包已改成 Animation/ 路径, 原文件备份为 .vtube.original.json。

安装(按 Mao README):
1. 把 da-feiyu/ 整个文件夹放进: 澪Agent应用\dist\live2d-pet\models\da-feiyu\
   (目录名小写即模型 id, 含 model3.json 即自动识别)
2. 在 dist\live2d-pet\live2d-pet.js 的 builtInModels 里登记 da-feiyu。
   如果你的 agent 是直接读模型目录(不是 Mao 内置列表), 第 2 步可跳过, 只要目录里有 model3.json 即可。
3. 分层 PSD 不在本包里: 原包 psd/大肥鱼公皮分享拆分.psd 已随之前 122MB 完整包发过, 需要再拿就说一声。

作者条款提醒: 仅自己用、做内容、改作; 不要把原包原样转售或上传到公开下载站。
