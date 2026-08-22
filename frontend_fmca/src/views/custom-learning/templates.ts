/**
 * 自定义学习的内置模板与演示数据（评审第 8 项：数据与组件分离）。
 *
 * - PLAN_TEMPLATES：首页「学习计划模板」区的人工整理模板；
 * - DEMO_PATH_PLANS：学习工作台在非 UUID 计划 id 下的内置演示路径；
 * - 本文件在 frontend / frontend_audit / frontend_fmca 三端必须逐字节一致，
 *   由 backend/tests/test_learning_views_consistency.py 契约保障。
 */

export type NodeStatus = 'open' | 'locked' | 'done';
export interface LearningObjective {
  title: string;
  description: string;
  rationale: string;
  checkpoints: string[];
}

export interface LearningResource {
  type: '教材' | '讲义' | '视频' | '数据';
  title: string;
  description: string;
  minutes: number;
}

export interface LearningProject {
  title: string;
  description: string;
  output: string;
}

export interface LearningNode {
  title: string;
  label: string;
  status: NodeStatus;
}

export interface LearningPlan {
  id: string;
  title: string;
  subtitle: string;
  duration: string;
  depth: string;
  courseType: string;
  progress: number;
  accent: string;
  soft: string;
  summary: string;
  objectives: LearningObjective[];
  resources: LearningResource[];
  projects: LearningProject[];
  nodes: LearningNode[];
  weeklyPlan: string[];
}

export type NodeState = 'open' | 'locked' | 'done' | 'learning' | 'review';
export interface PathNode {
  id: string;
  title: string;
  subtitle: string;
  day: string;
  state: NodeState;
  isReviewChild?: boolean;
  resource: string;
  task: string;
  check: string;
}

export interface PathPlan {
  id: string;
  title: string;
  category: string;
  duration: string;
  difficulty: string;
  accent: string;
  surface: string;
  description: string;
  outcomes: string[];
  nodes: PathNode[];
}

export const PLAN_TEMPLATES: LearningPlan[] = [
  {
    id: 'detector-principles',
    title: '探测器原理',
    subtitle: '从辐射相互作用到输出脉冲，建立探测器工作链路的完整理解。',
    duration: '5天',
    depth: '分析层',
    courseType: '核电子学',
    progress: 0,
    accent: '#2563eb',
    soft: '#eff6ff',
    summary: '适合希望快速补齐探测器物理基础、读懂前端电路需求的学习者。',
    objectives: [
      {
        title: '理解辐射与物质相互作用',
        description: '区分带电粒子、光子与中子在介质中的能量沉积机制。',
        rationale: '这是判断探测器材料、几何结构和读出方式的第一步。',
        checkpoints: ['能解释电离与激发差异', '能描述光电效应、康普顿散射和电子对效应', '能判断常见射线适合的探测介质'],
      },
      {
        title: '建立探测器信号形成模型',
        description: '把电荷收集、漂移、倍增与输出脉冲统一到一个时间响应模型中。',
        rationale: '信号形成模型决定前置放大器带宽、噪声和成形时间常数。',
        checkpoints: ['会画电荷收集过程', '能说明脉冲上升沿来源', '能解释成形时间与计数率的取舍'],
      },
      {
        title: '辨析典型探测器类型',
        description: '比较气体、闪烁体、半导体探测器的结构、优势和应用边界。',
        rationale: '不同探测器需要不同偏置、高压、安全和信号处理方案。',
        checkpoints: ['能完成三类探测器对比表', '能选择合适的能谱测量方案', '能识别饱和、死时间和温漂风险'],
      },
      {
        title: '连接探测器与核电子学系统',
        description: '从探测器输出推导前端放大、滤波、甄别和采集模块的指标。',
        rationale: '把物理信号翻译为电子学指标，是系统设计的关键能力。',
        checkpoints: ['能定义噪声、增益与动态范围', '能解释阈值设置原则', '能完成一个探测链路框图'],
      },
    ],
    resources: [
      { type: '教材', title: '辐射探测与测量基础章节', description: '能量沉积、探测效率与能量分辨率', minutes: 90 },
      { type: '讲义', title: '气体探测器与闪烁探测器对比', description: '结构、工作区间和读出方式', minutes: 45 },
      { type: '视频', title: '半导体探测器信号形成演示', description: '电荷漂移、收集和脉冲响应', minutes: 28 },
      { type: '数据', title: 'NaI(Tl) 能谱样例数据', description: '用于练习峰识别和分辨率估计', minutes: 35 },
    ],
    projects: [
      { title: '探测器选型卡片', description: '为三类测量任务选择探测器并写出理由。', output: '一页选型表' },
      { title: '脉冲形成草图', description: '把电荷收集过程画成等效电流脉冲与电压脉冲。', output: '信号形成示意图' },
      { title: '能谱指标计算', description: '根据样例数据估算能量分辨率与峰面积。', output: '计算记录表' },
    ],
    nodes: [
      { title: '辐射相互作用', label: '01', status: 'open' },
      { title: '电荷收集', label: '02', status: 'open' },
      { title: '脉冲形成', label: '03', status: 'open' },
      { title: '探测器类型', label: '04', status: 'open' },
      { title: '系统指标', label: '05', status: 'open' },
      { title: '综合设计', label: '06', status: 'open' },
    ],
    weeklyPlan: ['第1天：辐射相互作用与探测效率', '第2天：信号形成与电荷收集', '第3天：气体、闪烁体、半导体探测器', '第4天：电子学指标映射', '第5天：完成探测链路设计小项目'],
  },
  {
    id: 'position-sensing',
    title: '位置传感',
    subtitle: '聚焦辐射探测器位置读出的编码、插值与信号处理。',
    duration: '5天',
    depth: '分析层',
    courseType: '辐射探测器的信号处理',
    progress: 0,
    accent: '#0f766e',
    soft: '#ecfdf5',
    summary: '适合进入阵列读出、多通道采集和位置重建前的专项学习。',
    objectives: [
      { title: '理解位置灵敏探测器结构', description: '认识条形、像素、延迟线和电荷分配读出。', rationale: '结构决定读出通道数、空间分辨率和数据量。', checkpoints: ['能说明位置编码方式', '能比较条形与像素读出', '能识别电荷共享现象'] },
      { title: '掌握重心与插值算法', description: '用多通道幅值估计入射位置并判断误差来源。', rationale: '位置重建算法是硬件读出到物理量之间的桥梁。', checkpoints: ['能写出重心公式', '能解释边缘效应', '能评估噪声影响'] },
      { title: '设计多通道读出链路', description: '确定前端、采样、触发和同步方案。', rationale: '位置系统对通道一致性和同步精度更敏感。', checkpoints: ['能列出链路模块', '能定义采样率需求', '能说明校准流程'] },
      { title: '完成位置重建评估', description: '根据样例事件数据输出位置谱和分辨率估计。', rationale: '用指标验证算法和硬件设计是否满足任务要求。', checkpoints: ['能绘制位置直方图', '能计算FWHM', '能发现异常通道'] },
    ],
    resources: [
      { type: '讲义', title: '位置灵敏探测器读出结构', description: '条形、像素和电荷分配原理', minutes: 50 },
      { type: '数据', title: '四通道电荷分配事件表', description: '用于位置重心估计练习', minutes: 40 },
      { type: '视频', title: '多通道同步采集案例', description: '触发、时间戳和通道校准', minutes: 32 },
      { type: '教材', title: '空间分辨率与统计误差', description: '分辨率定义、误差传递与标定', minutes: 75 },
    ],
    projects: [
      { title: '重心算法小实验', description: '用给定事件表计算二维位置坐标。', output: '坐标散点图' },
      { title: '异常通道诊断', description: '识别增益偏差和噪声异常通道。', output: '诊断清单' },
      { title: '读出链路方案', description: '为条形探测器设计多通道采集链路。', output: '系统框图' },
    ],
    nodes: [
      { title: '结构与编码', label: '01', status: 'open' },
      { title: '电荷共享', label: '02', status: 'open' },
      { title: '重心算法', label: '03', status: 'open' },
      { title: '多通道读出', label: '04', status: 'locked' },
      { title: '标定流程', label: '05', status: 'locked' },
      { title: '分辨率评估', label: '06', status: 'locked' },
    ],
    weeklyPlan: ['第1天：位置灵敏探测器结构', '第2天：电荷分配与重心算法', '第3天：多通道采集与同步', '第4天：标定和误差来源', '第5天：位置重建小项目'],
  },
  {
    id: 'nuclear-decay',
    title: '核衰变规律',
    subtitle: '围绕半衰期、活度、衰变链和统计涨落建立计算能力。',
    duration: '7天',
    depth: '理论深入学习',
    courseType: '核电子学',
    progress: 0,
    accent: '#b45309',
    soft: '#fffbeb',
    summary: '适合在能谱分析、计数测量和实验安全评估前系统复习。',
    objectives: [
      { title: '掌握指数衰变模型', description: '理解衰变常数、半衰期和平均寿命之间的关系。', rationale: '这是活度计算和计数修正的数学基础。', checkpoints: ['能推导指数衰变公式', '能换算半衰期与衰变常数', '能处理单位'] },
      { title: '计算活度与计数率', description: '把源强、探测效率、几何效率和本底联系起来。', rationale: '实验读数需要经过效率和本底修正才能解释。', checkpoints: ['能完成活度估算', '能做本底扣除', '能说明效率修正'] },
      { title: '理解衰变链平衡', description: '识别瞬时平衡、长期平衡和无平衡场景。', rationale: '复杂样品常包含母子体贡献，直接影响谱峰解释。', checkpoints: ['能画衰变链', '能判断平衡类型', '能解释子体增长曲线'] },
      { title: '处理计数统计涨落', description: '用泊松统计评估不确定度和测量时间。', rationale: '低计数实验必须用统计置信度指导测量设计。', checkpoints: ['能计算标准差', '能估计测量时长', '能比较信噪比'] },
      { title: '完成衰变曲线拟合', description: '根据计数数据估计半衰期并报告误差。', rationale: '把理论模型落到真实数据处理。', checkpoints: ['能做半对数图', '能拟合斜率', '能写出结论'] },
    ],
    resources: [
      { type: '教材', title: '放射性衰变基础', description: '半衰期、活度和衰变链', minutes: 80 },
      { type: '数据', title: '短寿命核素计数序列', description: '用于拟合半衰期', minutes: 45 },
      { type: '讲义', title: '计数统计与不确定度', description: '泊松统计和误差传播', minutes: 55 },
      { type: '视频', title: '衰变链平衡动画讲解', description: '母体和子体活度变化', minutes: 22 },
    ],
    projects: [
      { title: '半衰期拟合', description: '用半对数图估计衰变常数。', output: '拟合曲线与报告' },
      { title: '计数时间规划', description: '按目标相对误差反推测量时间。', output: '实验计划表' },
      { title: '衰变链判断', description: '根据核素半衰期判断平衡状态。', output: '判定说明' },
    ],
    nodes: [
      { title: '指数模型', label: '01', status: 'open' },
      { title: '活度计算', label: '02', status: 'open' },
      { title: '效率修正', label: '03', status: 'open' },
      { title: '衰变链', label: '04', status: 'open' },
      { title: '泊松统计', label: '05', status: 'locked' },
      { title: '曲线拟合', label: '06', status: 'locked' },
    ],
    weeklyPlan: ['第1天：衰变常数与半衰期', '第2天：活度与计数率', '第3天：效率与本底修正', '第4天：衰变链平衡', '第5天：计数统计', '第6天：半衰期拟合', '第7天：综合练习与回顾'],
  },
  {
    id: 'digital-signals',
    title: '数字信号与系统导论',
    subtitle: '从采样、量化、滤波到频域分析，补齐数字核电子学基础。',
    duration: '8天',
    depth: '基础专项',
    courseType: '核电子学',
    progress: 0,
    accent: '#7c3aed',
    soft: '#f5f3ff',
    summary: '适合准备学习数字脉冲处理、FPGA 采集和软件谱分析的学习者。',
    objectives: [
      { title: '理解采样与量化', description: '掌握采样定理、混叠、量化误差和动态范围。', rationale: 'ADC 选择和信号保真都建立在这部分概念上。', checkpoints: ['能解释奈奎斯特频率', '能判断混叠风险', '能计算量化步长'] },
      { title: '掌握离散系统描述', description: '用差分方程、冲激响应和卷积描述离散系统。', rationale: '数字滤波器设计需要统一的系统语言。', checkpoints: ['能写差分方程', '能计算简单卷积', '能解释系统稳定性'] },
      { title: '进入频域分析', description: '使用 DFT/FFT 观察信号频谱和噪声分布。', rationale: '频域视角能帮助理解滤波、噪声和脉冲成形。', checkpoints: ['能解释频率分辨率', '能读幅度谱', '能识别窗函数影响'] },
      { title: '完成数字滤波练习', description: '设计一个简单平滑或成形滤波器并验证效果。', rationale: '把理论变成可复用的信号处理流程。', checkpoints: ['能比较滤波前后噪声', '能解释延迟', '能形成参数建议'] },
    ],
    resources: [
      { type: '教材', title: '数字信号处理入门章节', description: '采样、量化与离散系统', minutes: 100 },
      { type: '讲义', title: 'ADC 指标与核电子学采样', description: '位数、采样率和动态范围', minutes: 45 },
      { type: '视频', title: 'FFT 可视化演示', description: '频谱泄漏和窗函数', minutes: 30 },
      { type: '数据', title: '脉冲波形采样数据', description: '用于滤波与谱分析练习', minutes: 50 },
    ],
    projects: [
      { title: '采样率选择', description: '为给定脉冲宽度选择合适采样率。', output: '参数建议表' },
      { title: 'FFT 频谱观察', description: '对样例波形做频域分析。', output: '频谱图' },
      { title: '数字平滑滤波', description: '比较移动平均与简单成形滤波效果。', output: '滤波对比图' },
    ],
    nodes: [
      { title: '采样定理', label: '01', status: 'open' },
      { title: '量化误差', label: '02', status: 'open' },
      { title: '差分方程', label: '03', status: 'open' },
      { title: '卷积', label: '04', status: 'locked' },
      { title: 'DFT/FFT', label: '05', status: 'locked' },
      { title: '数字滤波', label: '06', status: 'locked' },
    ],
    weeklyPlan: ['第1天：采样与混叠', '第2天：量化与 ADC 指标', '第3天：离散系统', '第4天：卷积与冲激响应', '第5天：DFT 与 FFT', '第6天：窗函数与频谱泄漏', '第7天：数字滤波', '第8天：综合小项目'],
  },
  {
    id: 'semiconductor-detectors',
    title: '半导体探测器',
    subtitle: '围绕硅探测器、高纯锗探测器和读出噪声建立专项能力。',
    duration: '10天',
    depth: '进阶专项',
    courseType: '辐射探测器的信号处理',
    progress: 0,
    accent: '#db2777',
    soft: '#fdf2f8',
    summary: '适合对能谱分辨率、漏电流、偏置和低噪声读出有专项需求的学习者。',
    objectives: [
      { title: '理解 PN 结与耗尽区', description: '把半导体物理与探测器灵敏体积联系起来。', rationale: '耗尽区决定有效探测体积和偏置需求。', checkpoints: ['能解释耗尽区形成', '能说明偏置作用', '能判断击穿风险'] },
      { title: '分析噪声来源', description: '区分串联噪声、并联噪声、1/f 噪声和温度影响。', rationale: '低噪声读出是半导体探测器性能核心。', checkpoints: ['能列出噪声项', '能解释漏电流影响', '能选择成形时间'] },
      { title: '掌握电荷灵敏前放', description: '理解反馈电容、复位和输出阶跃响应。', rationale: '半导体探测器常与电荷灵敏前置放大器配合。', checkpoints: ['能画前放结构', '能计算电荷到电压转换', '能说明复位方式'] },
      { title: '熟悉能谱校准', description: '完成峰位、能量刻度和能量分辨率评估。', rationale: '能谱测量最终要落实到标定和谱峰解释。', checkpoints: ['能做线性刻度', '能计算FWHM', '能识别逃逸峰或叠加峰'] },
      { title: '形成实验安全清单', description: '整理高压、低温、接地和屏蔽注意事项。', rationale: '半导体探测器实验对环境和操作规范更敏感。', checkpoints: ['能列出操作步骤', '能说明高压联锁', '能设计接地方案'] },
    ],
    resources: [
      { type: '教材', title: '半导体辐射探测器章节', description: 'PN 结、耗尽层和电荷收集', minutes: 110 },
      { type: '讲义', title: '电荷灵敏前置放大器', description: '反馈、电荷转换和噪声模型', minutes: 60 },
      { type: '视频', title: 'HPGe 能谱系统搭建', description: '低温、高压和谱仪链路', minutes: 38 },
      { type: '数据', title: '多峰能谱样例', description: '用于能量刻度和分辨率评估', minutes: 55 },
    ],
    projects: [
      { title: '耗尽电压估算', description: '根据材料参数估算耗尽层厚度。', output: '计算表' },
      { title: '噪声预算', description: '比较不同成形时间下的噪声贡献。', output: '噪声预算图' },
      { title: '能谱校准', description: '用样例峰位完成线性能量刻度。', output: '校准曲线' },
    ],
    nodes: [
      { title: 'PN 结', label: '01', status: 'open' },
      { title: '耗尽区', label: '02', status: 'open' },
      { title: '电荷收集', label: '03', status: 'locked' },
      { title: '前置放大', label: '04', status: 'locked' },
      { title: '噪声模型', label: '05', status: 'locked' },
      { title: '能谱校准', label: '06', status: 'locked' },
    ],
    weeklyPlan: ['第1-2天：半导体基础与 PN 结', '第3天：耗尽区与偏置', '第4天：电荷收集', '第5-6天：电荷灵敏前放', '第7天：噪声模型', '第8天：能谱校准', '第9-10天：综合实验项目'],
  },
  {
    id: 'nuclear-electronics-basics',
    title: '核电子学基础',
    subtitle: '快速建立核电子学系统模块、指标与实验链路的全局认识。',
    duration: '10天',
    depth: '入门框架',
    courseType: '核电子学',
    progress: 0,
    accent: '#64748b',
    soft: '#f1f5f9',
    summary: '适合从零进入核电子学课程，或在进入专项学习前建立知识框架。',
    objectives: [
      { title: '认识核电子学系统链路', description: '从探测器、前放、主放、甄别到采集建立模块视图。', rationale: '先有系统地图，后续学习才不容易碎片化。', checkpoints: ['能画完整链路', '能说明各模块作用', '能识别模拟与数字边界'] },
      { title: '理解关键性能指标', description: '掌握增益、噪声、分辨率、死时间和计数率。', rationale: '指标是设计、调试和评价系统的共同语言。', checkpoints: ['能解释主要指标', '能判断指标冲突', '能给出测试方法'] },
      { title: '掌握基本脉冲处理', description: '理解放大、成形、阈值甄别和符合测量。', rationale: '脉冲处理决定能量、时间和计数信息质量。', checkpoints: ['能画成形前后波形', '能解释阈值选择', '能说明符合逻辑'] },
      { title: '了解数据采集架构', description: '熟悉 MCA、ADC、TDC、FPGA 和上位机的角色。', rationale: '现代核电子学越来越依赖数字采集和软件处理。', checkpoints: ['能区分 ADC 与 MCA', '能说明采集流程', '能描述数据格式'] },
      { title: '完成实验调试流程', description: '按步骤完成上电、信号检查、参数扫描和记录。', rationale: '工程实践能力来自规范调试，而不是只看框图。', checkpoints: ['能制定调试清单', '能记录异常', '能写实验日志'] },
      { title: '形成课程学习地图', description: '把后续专项课程放到系统框架中。', rationale: '让每个知识点都有位置，便于长期复习。', checkpoints: ['能整理知识图谱', '能选择下一门课程', '能制定复习计划'] },
    ],
    resources: [
      { type: '教材', title: '核电子学概论', description: '系统模块和核心指标', minutes: 120 },
      { type: '讲义', title: '模拟脉冲处理基础', description: '放大、成形和甄别', minutes: 70 },
      { type: '视频', title: 'MCA 能谱系统演示', description: '从源到谱图的完整流程', minutes: 35 },
      { type: '数据', title: '基础能谱与本底数据', description: '用于峰识别和本底扣除', minutes: 45 },
    ],
    projects: [
      { title: '系统链路图', description: '画出一套能谱测量系统并标出信号类型。', output: '链路框图' },
      { title: '指标卡片', description: '整理 8 个核心指标的定义、影响和测试方法。', output: '指标速查表' },
      { title: '调试清单', description: '编写实验上电和参数扫描步骤。', output: '调试 SOP' },
    ],
    nodes: [
      { title: '系统概览', label: '01', status: 'open' },
      { title: '核心指标', label: '02', status: 'open' },
      { title: '前置放大', label: '03', status: 'open' },
      { title: '成形甄别', label: '04', status: 'locked' },
      { title: '数据采集', label: '05', status: 'locked' },
      { title: '实验调试', label: '06', status: 'locked' },
    ],
    weeklyPlan: ['第1天：核电子学系统概览', '第2天：关键性能指标', '第3-4天：模拟脉冲处理', '第5天：甄别与符合', '第6-7天：数据采集', '第8天：实验调试', '第9-10天：综合复盘'],
  },
];

export const DEMO_PATH_PLANS: PathPlan[] = [
  {
    id: 'detector-principles',
    title: '探测器原理',
    category: '核电子学',
    duration: '5天',
    difficulty: '分析层',
    accent: '#2563eb',
    surface: '#eff6ff',
    description: '从辐射相互作用、信号形成到探测链路指标，建立探测器与前端电子学之间的完整映射。',
    outcomes: ['画出探测器到采集系统的链路图', '解释脉冲形成与成形时间常数', '完成探测器选型与能谱指标计算'],
    nodes: [
      { id: 'interaction', title: '辐射相互作用', subtitle: '电离、激发与光子三效应', day: 'Day 1', state: 'open', resource: '辐射探测与测量基础章节', task: '整理三类射线与介质作用表', check: '能判断常见射线适合的探测介质' },
      { id: 'charge', title: '电荷收集', subtitle: '漂移、扩散与收集效率', day: 'Day 2', state: 'open', resource: '电荷收集过程讲义', task: '画出载流子收集时序图', check: '能解释脉冲上升沿来源' },
      { id: 'pulse', title: '脉冲形成', subtitle: '电流脉冲到电压脉冲', day: 'Day 2', state: 'open', resource: '半导体探测器信号形成演示', task: '完成等效电流脉冲草图', check: '能说明成形时间与计数率取舍' },
      { id: 'types', title: '探测器类型', subtitle: '气体、闪烁体、半导体', day: 'Day 3', state: 'open', resource: '典型探测器对比讲义', task: '完成三类探测器对比卡片', check: '能选择合适的能谱测量方案' },
      { id: 'metrics', title: '系统指标', subtitle: '噪声、增益与动态范围', day: 'Day 4', state: 'open', resource: '前端电子学指标速查', task: '把探测器输出转换为电路指标', check: '能定义阈值、噪声和动态范围' },
      { id: 'design', title: '综合设计', subtitle: '探测链路小项目', day: 'Day 5', state: 'open', resource: 'NaI(Tl) 能谱样例数据', task: '提交一套探测系统链路方案', check: '能完成链路图与指标说明' },
      { id: 'calibration', title: '能量刻度', subtitle: '峰位、线性刻度与误差', day: 'Day 5', state: 'locked', resource: '能谱刻度练习数据', task: '完成峰位识别和线性刻度', check: '能给出刻度方程和残差说明' },
      { id: 'review', title: '知识复盘', subtitle: '链路、指标与应用场景', day: 'Day 6', state: 'locked', resource: '探测系统复盘清单', task: '整理一份探测器选型报告', check: '能独立解释完整探测链路' },
      { id: 'error-budget', title: '误差预算', subtitle: '效率、统计与系统误差', day: 'Day 6', state: 'locked', resource: '误差预算模板', task: '估算探测链路主要误差项', check: '能说明误差来源和改进方向' },
      { id: 'presentation', title: '实验答辩', subtitle: '方案表达与追问准备', day: 'Day 7', state: 'locked', resource: '探测器方案答辩清单', task: '完成 5 分钟方案汇报', check: '能清楚解释选型、指标和风险' },
    ],
  },
  {
    id: 'position-sensing',
    title: '位置传感',
    category: '辐射探测器的信号处理',
    duration: '5天',
    difficulty: '分析层',
    accent: '#0f766e',
    surface: '#ecfdf5',
    description: '围绕位置灵敏探测器结构、多通道读出和位置重建算法，完成从事件数据到位置谱的训练。',
    outcomes: ['解释条形、像素与电荷分配读出', '完成重心算法位置重建', '识别异常通道并提出校准流程'],
    nodes: [
      { id: 'structure', title: '结构与编码', subtitle: '条形、像素、延迟线', day: 'Day 1', state: 'open', resource: '位置灵敏探测器结构讲义', task: '整理三种位置编码方式', check: '能比较通道数与空间分辨率' },
      { id: 'sharing', title: '电荷共享', subtitle: '入射位置与多通道幅值', day: 'Day 2', state: 'open', resource: '电荷分配事件表', task: '分析相邻通道幅值关系', check: '能识别边缘效应' },
      { id: 'centroid', title: '重心算法', subtitle: '插值与误差来源', day: 'Day 2', state: 'open', resource: '位置重建算法练习', task: '计算二维位置坐标', check: '能写出重心公式并评估噪声影响' },
      { id: 'readout', title: '多通道读出', subtitle: '同步、触发、采样', day: 'Day 3', state: 'locked', resource: '多通道同步采集案例', task: '设计条形探测器读出链路', check: '能定义采样率与同步需求' },
      { id: 'calibration', title: '标定流程', subtitle: '增益一致性与位置刻度', day: 'Day 4', state: 'locked', resource: '空间分辨率与统计误差', task: '制定异常通道校准清单', check: '能发现增益偏差和噪声异常' },
      { id: 'resolution', title: '分辨率评估', subtitle: '直方图与 FWHM', day: 'Day 5', state: 'locked', resource: '位置谱样例数据', task: '输出位置谱和分辨率估计', check: '能计算 FWHM 并解释结果' },
      { id: 'correction', title: '非线性修正', subtitle: '边缘效应与校正曲线', day: 'Day 6', state: 'locked', resource: '位置非线性校正样例', task: '拟合位置校正曲线', check: '能说明非线性误差来源' },
      { id: 'imaging', title: '成像验证', subtitle: '二维图像与缺陷识别', day: 'Day 7', state: 'locked', resource: '二维位置事件数据', task: '生成位置分布图并标注异常', check: '能用图像验证读出链路效果' },
      { id: 'integration', title: '系统联调', subtitle: '读出链路与算法联测', day: 'Day 8', state: 'locked', resource: '位置读出联调记录表', task: '完成一次端到端联调', check: '能定位硬件、算法或标定问题' },
      { id: 'position-report', title: '结果汇报', subtitle: '位置谱与分辨率结论', day: 'Day 9', state: 'locked', resource: '位置重建报告模板', task: '提交位置重建分析报告', check: '能说明分辨率和异常通道处理' },
    ],
  },
  {
    id: 'nuclear-decay',
    title: '核衰变规律',
    category: '核电子学',
    duration: '7天',
    difficulty: '理论深入学习',
    accent: '#b45309',
    surface: '#fffbeb',
    description: '从指数衰变、活度计算、衰变链平衡到计数统计，形成可用于实验设计的数据计算能力。',
    outcomes: ['推导并使用指数衰变公式', '完成活度、本底和效率修正', '根据计数序列拟合半衰期'],
    nodes: [
      { id: 'model', title: '指数模型', subtitle: '半衰期与衰变常数', day: 'Day 1', state: 'open', resource: '放射性衰变基础', task: '推导指数衰变公式', check: '能换算半衰期与平均寿命' },
      { id: 'activity', title: '活度计算', subtitle: '源强、效率与计数率', day: 'Day 2', state: 'open', resource: '活度计算例题', task: '完成源强估算', check: '能处理单位和效率修正' },
      { id: 'background', title: '本底修正', subtitle: '净计数与测量误差', day: 'Day 3', state: 'open', resource: '本底扣除讲义', task: '计算净计数率', check: '能说明本底扣除影响' },
      { id: 'chain', title: '衰变链', subtitle: '母体、子体和平衡', day: 'Day 4', state: 'open', resource: '衰变链平衡动画', task: '判断平衡类型', check: '能解释子体增长曲线' },
      { id: 'poisson', title: '泊松统计', subtitle: '不确定度与测量时间', day: 'Day 5', state: 'locked', resource: '计数统计与不确定度', task: '按目标误差反推测量时长', check: '能比较信噪比' },
      { id: 'fit', title: '曲线拟合', subtitle: '半对数图与斜率', day: 'Day 6-7', state: 'locked', resource: '短寿命核素计数序列', task: '拟合半衰期并报告误差', check: '能写出拟合结论' },
      { id: 'branching', title: '分支比', subtitle: '分支衰变与谱线强度', day: 'Day 7', state: 'locked', resource: '分支比与谱线强度讲义', task: '根据分支比估算谱线贡献', check: '能区分活度和谱线强度' },
      { id: 'application', title: '应用案例', subtitle: '样品测量与安全评估', day: 'Day 8', state: 'locked', resource: '样品活度评估案例', task: '完成一个样品衰变计算报告', check: '能给出测量策略和风险说明' },
      { id: 'uncertainty', title: '不确定度合成', subtitle: '统计与系统误差合并', day: 'Day 8', state: 'locked', resource: '不确定度合成讲义', task: '合成活度计算不确定度', check: '能写出完整误差表达' },
      { id: 'decay-review', title: '案例复盘', subtitle: '衰变计算完整链路', day: 'Day 9', state: 'locked', resource: '衰变计算复盘清单', task: '复盘一个完整测量案例', check: '能独立完成衰变规律应用分析' },
    ],
  },
  {
    id: 'digital-signals',
    title: '数字信号与系统导论',
    category: '核电子学',
    duration: '8天',
    difficulty: '基础专项',
    accent: '#7c3aed',
    surface: '#f5f3ff',
    description: '补齐采样、量化、离散系统、频域分析与数字滤波，为数字核电子学和 FPGA 采集做准备。',
    outcomes: ['判断采样率与混叠风险', '用 DFT/FFT 观察频谱', '完成简单数字滤波对比'],
    nodes: [
      { id: 'sampling', title: '采样定理', subtitle: '奈奎斯特与混叠', day: 'Day 1', state: 'open', resource: '数字信号处理入门', task: '为脉冲宽度选择采样率', check: '能解释奈奎斯特频率' },
      { id: 'quant', title: '量化误差', subtitle: 'ADC 位数与动态范围', day: 'Day 2', state: 'open', resource: 'ADC 指标讲义', task: '计算量化步长', check: '能判断动态范围是否足够' },
      { id: 'system', title: '差分方程', subtitle: '离散系统描述', day: 'Day 3', state: 'open', resource: '离散系统讲义', task: '写出简单系统差分方程', check: '能解释系统稳定性' },
      { id: 'conv', title: '卷积', subtitle: '冲激响应与输出', day: 'Day 4', state: 'locked', resource: '卷积可视化练习', task: '完成简单卷积计算', check: '能连接冲激响应和输出' },
      { id: 'fft', title: 'DFT/FFT', subtitle: '频率分辨率与窗函数', day: 'Day 5-6', state: 'locked', resource: 'FFT 可视化演示', task: '输出脉冲波形频谱', check: '能识别频谱泄漏' },
      { id: 'filter', title: '数字滤波', subtitle: '平滑与成形', day: 'Day 7-8', state: 'locked', resource: '脉冲波形采样数据', task: '比较滤波前后噪声', check: '能给出滤波参数建议' },
      { id: 'trigger', title: '数字触发', subtitle: '阈值、时间戳与事件筛选', day: 'Day 8', state: 'locked', resource: '数字触发逻辑讲义', task: '设计一个简单触发判据', check: '能说明误触发和漏触发风险' },
      { id: 'pipeline', title: '处理流水线', subtitle: '从采样到谱图输出', day: 'Day 9', state: 'locked', resource: '数字脉冲处理流程案例', task: '整理完整处理流水线', check: '能串联采样、滤波、触发和统计' },
      { id: 'parameter-scan', title: '参数扫描', subtitle: '阈值、窗长与平滑阶数', day: 'Day 10', state: 'locked', resource: '数字处理参数扫描表', task: '比较不同参数组合效果', check: '能选择稳定的处理参数' },
      { id: 'system-output', title: '谱图输出', subtitle: '事件统计与结果展示', day: 'Day 11', state: 'locked', resource: '谱图输出案例', task: '输出一份数字谱分析结果', check: '能解释处理流程对谱图的影响' },
    ],
  },
];
