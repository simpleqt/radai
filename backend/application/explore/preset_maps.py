# -*- coding: utf-8 -*-
"""Preset mindmaps for fast instant retrieval."""

PRESET_MAPS = {
    "核电子学基础": {
        "root": {
            "title": "核电子学基础",
            "summary": "研究核辐射探测、信号处理及测量系统的基本理论与技术。"
        },
        "children": [
            {
                "title": "辐射探测原理",
                "summary": "阐述核辐射与物质相互作用的机理及其在探测中的意义。",
                "expandable": True
            },
            {
                "title": "探测器结构与类型",
                "summary": "介绍常见探测器的工作原理、结构特点及适用场景。",
                "expandable": True
            },
            {
                "title": "信号成形与放大",
                "summary": "描述前放、成形电路及噪声抑制技术以获得可靠信号。",
                "expandable": True
            },
            {
                "title": "能量与时间测量",
                "summary": "讲解能量谱和时间测量的原理、校正方法及误差分析。",
                "expandable": True
            },
            {
                "title": "数据获取与处理",
                "summary": "概述DAQ系统、事件触发、数据压缩及在线分析的实现。",
                "expandable": True
            },
            {
                "title": "核仪器校准与质量保证",
                "summary": "阐述能量、时间等参数的校准流程及质量控制措施。",
                "expandable": True
            }
        ]
    },
    "前置放大器": {
        "root": {
            "title": "前置放大器",
            "summary": "前置放大器将核探测器产生的微弱电荷信号放大并保持低噪声"
        },
        "children": [
            {
                "title": "工作原理",
                "summary": "将检测器产生的微弱电荷积分并转换为可处理的电压信号",
                "expandable": True
            },
            {
                "title": "电路结构",
                "summary": "包括输入级、反馈网络、偏置电路等关键模块",
                "expandable": True
            },
            {
                "title": "噪声特性",
                "summary": "分析热噪声、闪烁噪声等对信号质量的影响",
                "expandable": True
            },
            {
                "title": "频率响应",
                "summary": "决定脉冲上升时间与信号带宽的关键因素",
                "expandable": True
            },
            {
                "title": "线性与动态范围",
                "summary": "描述放大器对不同幅度信号的保真度与饱和行为",
                "expandable": True
            },
            {
                "title": "稳定性与温度漂移",
                "summary": "探讨偏置漂移、温度系数及补偿技术",
                "expandable": True
            }
        ]
    },
    "主放大器": {
        "root": {
            "title": "主放大器",
            "summary": "用于放大核电子学中探测信号的电子电路"
        },
        "children": [
            {
                "title": "基本原理",
                "summary": "阐述放大器的工作机制与核心物理概念",
                "expandable": True
            },
            {
                "title": "电路结构",
                "summary": "描述典型主放大器的电路拓扑与元件布局",
                "expandable": True
            },
            {
                "title": "增益与带宽",
                "summary": "分析放大器的电压增益、功率增益与频率响应",
                "expandable": True
            },
            {
                "title": "噪声特性",
                "summary": "讨论内部噪声源及其对信号质量的影响",
                "expandable": True
            },
            {
                "title": "稳定性与反馈",
                "summary": "评估反馈网络对放大器稳定性和线性度的作用",
                "expandable": True
            },
            {
                "title": "接口与保护",
                "summary": "说明输入输出耦合方式与过载保护措施",
                "expandable": True
            }
        ]
    },
    "脉冲成形电路": {
        "root": {
            "title": "脉冲成形电路",
            "summary": "用于将探测器的原始电流脉冲转换为适合测量与分析的成形脉冲的电子电路。",
            "expandable": False
        },
        "children": [
            {
                "title": "脉冲成形原理",
                "summary": "通过积分、微分或滤波改变脉冲的时间宽度和形状。",
                "expandable": True
            },
            {
                "title": "电路拓扑结构",
                "summary": "包括RC、CR、LC、运算放大器有源成形等基本结构。",
                "expandable": True
            },
            {
                "title": "关键参数与指标",
                "summary": "时间常数、阶数、噪声、分辨率、线性度等决定成形性能。",
                "expandable": True
            },
            {
                "title": "设计要点",
                "summary": "选择合适的时间常数、带宽、增益和温度补偿，确保稳定性。",
                "expandable": True
            },
            {
                "title": "性能评估方法",
                "summary": "通过噪声谱、脉冲响应、能量分辨率和时间分辨率测量验证。",
                "expandable": True
            },
            {
                "title": "典型应用场景",
                "summary": "核谱仪、粒子探测器、计时电路和触发系统中广泛使用。",
                "expandable": True
            }
        ]
    },
    "多道分析器": {
        "root": {
            "title": "多道分析器",
            "summary": "用于核辐射能谱测量的脉冲幅度分析仪器，将事件按能量分配到不同通道并生成能谱。"
        },
        "children": [
            {
                "title": "工作原理",
                "summary": "将输入脉冲幅度转换为数字量，按能量分配到对应通道形成能谱。",
                "expandable": True
            },
            {
                "title": "硬件结构",
                "summary": "包括前端放大、成形、模数转换、存储与控制电路等关键模块。",
                "expandable": True
            },
            {
                "title": "数据采集模式",
                "summary": "支持直方图模式和列表模式，分别用于能谱显示和原始事件记录。",
                "expandable": True
            },
            {
                "title": "能量校准与稳定性",
                "summary": "通过已知能量峰进行校准，确保通道与实际能量的对应准确。",
                "expandable": True
            },
            {
                "title": "性能指标",
                "summary": "关键指标包括能量分辨率、计数率、死时间及线性范围。",
                "expandable": True
            },
            {
                "title": "应用与数据处理",
                "summary": "广泛用于γ谱、X射线谱分析，配以后处理软件实现峰识别与定量。",
                "expandable": True
            }
        ]
    },
    "甄别器与定时": {
        "root": {
            "title": "甄别器与定时",
            "summary": "核电子学中用于信号判别与时间标记的关键技术。"
        },
        "children": [
            {
                "title": "甄别器原理",
                "summary": "解释阈值触发和信号判别的基本机制。",
                "expandable": True
            },
            {
                "title": "甄别器类型",
                "summary": "常见前沿、恒比、迟滞等甄别器及其特点。",
                "expandable": True
            },
            {
                "title": "定时技术",
                "summary": "包括前沿定时、恒比定时等实现方式。",
                "expandable": True
            },
            {
                "title": "定时误差与抖动",
                "summary": "描述时间分辨率、抖动来源及测量方法。",
                "expandable": True
            },
            {
                "title": "电路实现与校准",
                "summary": "硬件电路设计、阈值校准与温度补偿。",
                "expandable": True
            },
            {
                "title": "应用与性能评估",
                "summary": "在粒子探测系统中如何评估和使用。",
                "expandable": True
            }
        ]
    },
    "符合与反符合": {
        "root": {
            "title": "符合与反符合",
            "summary": "研究核电子学中信号的时间对应与排斥关系"
        },
        "children": [
            {
                "title": "基本原理",
                "summary": "阐述符合与反符合的时间窗口概念及判定逻辑。",
                "expandable": True
            },
            {
                "title": "电路实现",
                "summary": "介绍实现符合/反符合逻辑的硬件电路与关键器件。",
                "expandable": True
            },
            {
                "title": "时间分辨率与窗口",
                "summary": "讨论时间分辨率、窗口宽度对符合率与误判的影响。",
                "expandable": True
            },
            {
                "title": "抗噪与反符合技术",
                "summary": "利用反符合抑制噪声和随机符合，提高信号纯度。",
                "expandable": True
            },
            {
                "title": "应用场景",
                "summary": "列举核物理、医学成像等领域中符合与反符合的典型应用。",
                "expandable": True
            },
            {
                "title": "数据处理与分析",
                "summary": "阐述离线数据中符合/反符合事件的筛选、校正与统计方法。",
                "expandable": True
            }
        ]
    },
    "模数转换": {
        "root": {
            "title": "模数转换",
            "summary": "将模拟信号转换为数字信号的过程与技术。"
        },
        "children": [
            {
                "title": "采样原理",
                "summary": "通过采样将连续信号离散化。",
                "expandable": True
            },
            {
                "title": "量化与编码",
                "summary": "把采样值映射为有限位数字并编码。",
                "expandable": True
            },
            {
                "title": "ADC架构",
                "summary": "常见类型包括逐次逼近、积分、流水线等。",
                "expandable": True
            },
            {
                "title": "性能指标",
                "summary": "关键参数有分辨率、采样率、信噪比等。",
                "expandable": True
            },
            {
                "title": "误差与噪声",
                "summary": "包括量化误差、时钟抖动、模拟噪声等。",
                "expandable": True
            },
            {
                "title": "实际应用",
                "summary": "广泛用于测量、通信、控制和传感等领域。",
                "expandable": True
            }
        ]
    },
    "数字脉冲处理": {
        "root": {
            "title": "数字脉冲处理",
            "summary": "对核探测器产生的脉冲信号进行采集、成形、测量和分析的关键技术。"
        },
        "children": [
            {
                "title": "脉冲采集与数字化",
                "summary": "将模拟脉冲信号转换为数字样本，为后续处理提供数据基础。",
                "expandable": True
            },
            {
                "title": "脉冲成形与滤波",
                "summary": "通过滤波和成形提升信噪比，塑造符合测量需求的脉冲形状。",
                "expandable": True
            },
            {
                "title": "时间测量与定时",
                "summary": "精确测量脉冲到达时间，实现事件时间戳和符合测量。",
                "expandable": True
            },
            {
                "title": "能量测量与谱分析",
                "summary": "提取脉冲幅度信息，构建能谱并进行定量分析。",
                "expandable": True
            },
            {
                "title": "噪声抑制与基线恢复",
                "summary": "降低电子噪声和基线漂移，提高测量精度和稳定性。",
                "expandable": True
            },
            {
                "title": "数据压缩与事件识别",
                "summary": "压缩存储数据，识别并剔除无效或重叠事件。",
                "expandable": True
            }
        ]
    },
    "探测器原理": {
        "root": {
            "title": "探测器原理",
            "summary": "阐述核辐射探测器实现信号生成与测量的基本原理。"
        },
        "children": [
            {
                "title": "辐射与物质相互作用",
                "summary": "辐射在介质中产生电离激发或散射等相互作用。",
                "expandable": True
            },
            {
                "title": "信号产生机制",
                "summary": "基于电离或闪烁等过程将辐射能量转化为电荷或光子。",
                "expandable": True
            },
            {
                "title": "电荷光收集",
                "summary": "通过电场或光电倍增管收集并放大产生的电荷或光信号。",
                "expandable": True
            },
            {
                "title": "探测器类型",
                "summary": "常见类型包括气体、闪烁体、半导体和切伦科夫探测器等。",
                "expandable": True
            },
            {
                "title": "性能评价指标",
                "summary": "关键指标有能量分辨率、时间分辨率、探测效率与噪声水平。",
                "expandable": True
            },
            {
                "title": "读出与电子学",
                "summary": "读出电路负责脉冲成形、放大、数字化及后续信号处理。",
                "expandable": True
            }
        ]
    },
    "闪烁探测器": {
        "root": {
            "title": "闪烁探测器",
            "summary": "利用闪烁体将辐射转化为光信号并检测的探测器。"
        },
        "children": [
            {
                "title": "工作原理",
                "summary": "辐射在闪烁体中产生激发态，退激后发射可见光。",
                "expandable": True
            },
            {
                "title": "闪烁体材料",
                "summary": "包括无机、有机和复合闪烁体，各具光产额、衰减时间等特性。",
                "expandable": True
            },
            {
                "title": "光电转换器件",
                "summary": "光电倍增管、雪崩光电二极管或硅光电倍增管等实现光到电的转换。",
                "expandable": True
            },
            {
                "title": "信号读取与处理",
                "summary": "包括前置放大、成形、数字化及噪声抑制等电子学环节。",
                "expandable": True
            },
            {
                "title": "性能指标与校准",
                "summary": "能量分辨率、时间分辨率、效率等关键指标及校准方法。",
                "expandable": True
            },
            {
                "title": "应用领域",
                "summary": "核物理、医学成像、安检、辐射监测等广泛场景。",
                "expandable": True
            }
        ]
    },
    "半导体探测器": {
        "root": {
            "title": "半导体探测器",
            "summary": "利用半导体材料实现粒子或光子能量测量的探测器"
        },
        "children": [
            {
                "title": "工作原理",
                "summary": "通过半导体材料中电子-空穴对产生信号",
                "expandable": True
            },
            {
                "title": "探测器类型",
                "summary": "常见的有PIN、CCD、硅漂移探测器等",
                "expandable": True
            },
            {
                "title": "关键参数",
                "summary": "包括能量分辨率、探测效率、噪声与时间响应",
                "expandable": True
            },
            {
                "title": "读出电路",
                "summary": "前置放大、滤波、模数转换等信号处理",
                "expandable": True
            },
            {
                "title": "材料工艺",
                "summary": "硅、锗、CdZnTe等材料的掺杂与微纳加工技术",
                "expandable": True
            },
            {
                "title": "应用领域",
                "summary": "核物理、天文观测、医学成像与辐射监测",
                "expandable": True
            }
        ]
    },
    "核辐射测量系统": {
        "root": {
            "title": "核辐射测量系统",
            "summary": "用于测量核辐射强度、能量和时间特性的完整仪器系统。"
        },
        "children": [
            {
                "title": "探测器原理",
                "summary": "阐述核辐射与物质相互作用的探测机理，如电离、闪烁和半导体效应。",
                "expandable": True
            },
            {
                "title": "探测器件",
                "summary": "介绍常用的气体、闪烁体和半导体探测器及其结构与工作方式。",
                "expandable": True
            },
            {
                "title": "电子学与信号处理",
                "summary": "包括高压供电、前置放大、成形放大及多道分析器的信号处理流程。",
                "expandable": True
            },
            {
                "title": "数据获取与分析",
                "summary": "涉及实时数据采集、存储、谱图生成及统计处理与结果解释。",
                "expandable": True
            },
            {
                "title": "校准与质量保证",
                "summary": "描述能量校准、效率刻度及质量控制方法以确保测量准确性。",
                "expandable": True
            },
            {
                "title": "屏蔽与几何布置",
                "summary": "讨论铅室、准直器及样品位置对测量灵敏度和本底的影响。",
                "expandable": True
            }
        ]
    },
    "能量分辨率": {
        "root": {
            "title": "能量分辨率",
            "summary": "核电子学中衡量探测器区分不同能量辐射的能力的关键指标"
        },
        "children": [
            {
                "title": "探测器类型",
                "summary": "不同探测器（半导体、闪烁体等）固有分辨率差异显著",
                "expandable": True
            },
            {
                "title": "统计噪声",
                "summary": "辐射产生的载流子数波动，受Fano因子影响",
                "expandable": True
            },
            {
                "title": "电子噪声",
                "summary": "前置放大器、滤波等电子元件引入的噪声",
                "expandable": True
            },
            {
                "title": "收集效率",
                "summary": "光或电荷在探测器内部的收集程度，损失降低分辨率",
                "expandable": True
            },
            {
                "title": "信号处理",
                "summary": "放大、成形、滤波等手段优化信噪比",
                "expandable": True
            },
            {
                "title": "校准与线性",
                "summary": "能量刻度校准及非线性校正提升分辨率",
                "expandable": True
            }
        ]
    },
    "能谱分析方法": {
        "root": {
            "title": "能谱分析方法",
            "summary": "研究核辐射能谱的获取、校准、峰识别及定量分析的理论与实践。"
        },
        "children": [
            {
                "title": "能量刻度与校准",
                "summary": "通过已知放射源建立能量与道址对应关系，实现能谱能量刻度。",
                "expandable": True
            },
            {
                "title": "谱获取与预处理",
                "summary": "采集原始计数并进行基线校正、死时间校正等预处理，为后续分析奠定数据基础。",
                "expandable": True
            },
            {
                "title": "峰识别与归属",
                "summary": "利用峰搜索算法定位能峰，结合核素库进行峰归属与谱线识别。",
                "expandable": True
            },
            {
                "title": "峰形与分辨率",
                "summary": "分析峰形宽度与对称性，评估探测器能量分辨率及其对谱的影响。",
                "expandable": True
            },
            {
                "title": "定量分析",
                "summary": "基于峰面积或峰高，采用效率刻度与衰变校正实现核素活度定量。",
                "expandable": True
            },
            {
                "title": "统计与误差评估",
                "summary": "运用泊松统计与蒙特卡洛方法评估计数误差和系统不确定度。",
                "expandable": True
            }
        ]
    },
    "符合定时技术": {
        "root": {
            "title": "符合定时技术",
            "summary": "研究核电子系统中符合事件的精确时间判定技术"
        },
        "children": [
            {
                "title": "符合定时原理",
                "summary": "阐述符合事件的时间判定基本原理",
                "expandable": True
            },
            {
                "title": "定时电路实现",
                "summary": "介绍实现符合定时的关键电路结构",
                "expandable": True
            },
            {
                "title": "时间分辨率",
                "summary": "衡量符合定时系统的时间分辨能力",
                "expandable": True
            },
            {
                "title": "误差来源与校正",
                "summary": "分析影响定时精度的误差因素及校正方法",
                "expandable": True
            },
            {
                "title": "典型应用",
                "summary": "列举核医学、天体物理等实际应用场景",
                "expandable": True
            },
            {
                "title": "前沿技术与发展",
                "summary": "探讨新型探测器和数字定时技术的进展",
                "expandable": True
            }
        ]
    },
    "辐射剂量测量": {
        "root": {
            "title": "辐射剂量测量",
            "summary": "辐射剂量测量是评估人体或材料受电离辐射照射程度的科学方法与技术。"
        },
        "children": [
            {
                "title": "基本概念与单位",
                "summary": "介绍吸收剂量、剂量当量等关键概念及其国际单位。",
                "expandable": True
            },
            {
                "title": "剂量测量原理",
                "summary": "阐述电离、荧光、热释光等剂量测量基本物理原理。",
                "expandable": True
            },
            {
                "title": "探测器类型与技术",
                "summary": "比较气体、闪烁、半导体等探测器的结构、工作方式与适用场景。",
                "expandable": True
            },
            {
                "title": "剂量率与累积剂量测量",
                "summary": "说明如何实时测量剂量率并积分得到累积剂量的技术与方法。",
                "expandable": True
            },
            {
                "title": "校准与质量保证",
                "summary": "介绍剂量测量系统的校准流程、质量控制措施及误差评估。",
                "expandable": True
            },
            {
                "title": "应用场景与辐射防护",
                "summary": "阐述医疗、工业、科研等领域剂量测量需求及防护原则。",
                "expandable": True
            }
        ]
    },
    "辐射防护": {
        "root": {
            "title": "辐射防护",
            "summary": "研究降低人类和环境免受电离及非电离辐射的有害影响。"
        },
        "children": [
            {
                "title": "辐射来源与分类",
                "summary": "介绍自然与人工辐射来源，区分电离与非电离辐射。",
                "expandable": True
            },
            {
                "title": "剂量学与测量",
                "summary": "阐述剂量概念、剂量单位和辐射测量仪器的基本原理。",
                "expandable": True
            },
            {
                "title": "防护原则与标准",
                "summary": "说明时间、距离和屏蔽三大防护原则及国际防护标准。",
                "expandable": True
            },
            {
                "title": "防护技术与设施",
                "summary": "介绍屏蔽材料、防护结构和安全设施的设计要点。",
                "expandable": True
            },
            {
                "title": "健康效应与风险",
                "summary": "阐述辐射对生物体的急慢性健康影响及风险评估方法。",
                "expandable": True
            },
            {
                "title": "法规监管与管理",
                "summary": "概述国家法规、监管机构职责及辐射安全管理制度。",
                "expandable": True
            }
        ]
    },
    "电荷灵敏放大": {
        "root": {
            "title": "电荷灵敏放大",
            "summary": "将检测器产生的电荷积分在反馈电容上，输出与电荷成正比的电压信号。"
        },
        "children": [
            {
                "title": "基本原理",
                "summary": "利用反馈电容将输入电荷转换为电压，实现积分放大。",
                "expandable": True
            },
            {
                "title": "电路结构",
                "summary": "包括积分电容、反馈网络、输入级和输出缓冲等关键模块。",
                "expandable": True
            },
            {
                "title": "关键参数",
                "summary": "包括增益、带宽、噪声、线性范围和温度稳定性等指标。",
                "expandable": True
            },
            {
                "title": "噪声特性",
                "summary": "主要来源于输入晶体管的热噪声和闪烁噪声，需优化设计降低噪声。",
                "expandable": True
            },
            {
                "title": "应用场景",
                "summary": "广泛用于粒子探测器、光电倍增管和医学成像等高灵敏度测量系统。",
                "expandable": True
            },
            {
                "title": "设计考虑",
                "summary": "涉及输入级拓扑、反馈网络选择、功耗与温度补偿等设计要点。",
                "expandable": True
            }
        ]
    },
    "噪声与信噪比": {
        "root": {
            "title": "噪声与信噪比",
            "summary": "研究核电子系统中噪声来源、特性及信噪比提升方法"
        },
        "children": [
            {
                "title": "噪声来源与类型",
                "summary": "阐述电子噪声、探测器噪声及环境噪声的主要来源与分类",
                "expandable": True
            },
            {
                "title": "噪声特性与表征",
                "summary": "介绍噪声功率谱、噪声系数及噪声温度等关键特性",
                "expandable": True
            },
            {
                "title": "信噪比定义与计算",
                "summary": "阐述信噪比的基本定义及其在时域和频域的计算方法",
                "expandable": True
            },
            {
                "title": "提高信噪比的方法",
                "summary": "讨论屏蔽、滤波、放大、累积平均等提升信噪比的技术",
                "expandable": True
            },
            {
                "title": "噪声对系统性能的影响",
                "summary": "分析噪声对能量分辨率、计数率及探测极限的影响",
                "expandable": True
            },
            {
                "title": "噪声测量与分析技术",
                "summary": "介绍噪声谱分析、噪声系数计量及实验测量方法",
                "expandable": True
            }
        ]
    },
    "中子探测技术": {
        "root": {
            "title": "中子探测技术",
            "summary": "利用中子与物质相互作用实现中子探测的技术与方法"
        },
        "children": [
            {
                "title": "中子相互作用原理",
                "summary": "阐述中子与原子核的散射、俘获等基本相互作用过程",
                "expandable": True
            },
            {
                "title": "探测器类型",
                "summary": "介绍气体、闪烁、半导体等常用中子探测器的结构与原理",
                "expandable": True
            },
            {
                "title": "探测器材料",
                "summary": "阐述用于高效中子俘获的关键材料如3He、BF3、Li-6等",
                "expandable": True
            },
            {
                "title": "电子学与信号处理",
                "summary": "描述前放、放大、成形、脉冲高度分析等信号处理技术",
                "expandable": True
            },
            {
                "title": "校准与剂量学",
                "summary": "讲解中子能谱、剂量转换系数及校准方法",
                "expandable": True
            },
            {
                "title": "应用与场景",
                "summary": "涵盖核物理实验、辐射防护、核设施监测等实际应用",
                "expandable": True
            }
        ]
    },
    "时间幅度变换": {
        "root": {
            "title": "时间幅度变换",
            "summary": "将时间间隔转换为幅度信号，实现核电子学中的时间测量与脉冲分析。"
        },
        "children": [
            {
                "title": "基本原理",
                "summary": "阐述时间间隔与幅度信号之间的线性或非线性映射关系。",
                "expandable": True
            },
            {
                "title": "电路实现",
                "summary": "介绍实现时间幅度变换的关键电路结构与工作原理。",
                "expandable": True
            },
            {
                "title": "时间幅度转换器",
                "summary": "描述专用时间-幅度转换器（TAC）的构成与工作流程。",
                "expandable": True
            },
            {
                "title": "性能指标",
                "summary": "列举时间分辨率、线性度、动态范围等关键性能参数。",
                "expandable": True
            },
            {
                "title": "误差与校正",
                "summary": "分析时钟抖动、漂移等误差来源并介绍校正方法。",
                "expandable": True
            },
            {
                "title": "应用场景",
                "summary": "展示在核谱仪、时间飞行测量及符合测量中的典型应用。",
                "expandable": True
            }
        ]
    },
    "数据获取系统": {
        "root": {
            "title": "数据获取系统",
            "summary": "用于在核电子实验中采集、转换、传输和存储探测器信号的完整系统。"
        },
        "children": [
            {
                "title": "探测器/传感器",
                "summary": "将核辐射转换为电信号，是系统的原始信号来源。",
                "expandable": True
            },
            {
                "title": "前置放大与信号调理",
                "summary": "对微弱信号进行放大、成形和滤波，以适配后续ADC的输入范围。",
                "expandable": True
            },
            {
                "title": "模数转换",
                "summary": "把模拟信号离散化为数字量，供后续处理和存储。",
                "expandable": True
            },
            {
                "title": "触发与时间标记",
                "summary": "根据阈值或模式产生触发信号，并为每个事件打上精确时间戳。",
                "expandable": True
            },
            {
                "title": "数据传输与存储",
                "summary": "将采集的数据实时传输至缓存或永久存储，确保数据完整性和可追溯性。",
                "expandable": True
            },
            {
                "title": "系统控制与软件接口",
                "summary": "提供用户交互、参数配置及实时监控功能，完成系统的整体调度。",
                "expandable": True
            }
        ]
    },
    "高压电源": {
        "root": {
            "title": "高压电源",
            "summary": "高压电源为核电子学中提供数百至数千伏稳定电压的关键装置。"
        },
        "children": [
            {
                "title": "高压产生原理",
                "summary": "阐述利用变压器、倍压电路或开关电源实现高压的原理。",
                "expandable": True
            },
            {
                "title": "电源拓扑结构",
                "summary": "介绍常见的升压、Cockcroft‑Walton、磁耦合等高压电源拓扑。",
                "expandable": True
            },
            {
                "title": "稳压与调节",
                "summary": "讨论使用反馈、参考电压和数字控制实现输出电压的稳定和调节。",
                "expandable": True
            },
            {
                "title": "安全与保护",
                "summary": "说明绝缘、屏蔽、过流和过压保护措施以确保操作安全。",
                "expandable": True
            },
            {
                "title": "滤波与纹波",
                "summary": "阐述使用电容、滤波网络和纹波抑制技术降低输出噪声。",
                "expandable": True
            },
            {
                "title": "测试与调试",
                "summary": "介绍高压电源的电压测量、负载特性和调试方法。",
                "expandable": True
            }
        ]
    },
    "标准核仪器": {
        "root": {
            "title": "标准核仪器",
            "summary": "标准核仪器是核物理与辐射测量的核心探测、信号处理、数据获取与校准设备。"
        },
        "children": [
            {
                "title": "探测器类型与原理",
                "summary": "介绍常用核辐射探测器的结构、工作原理及适用场景。",
                "expandable": True
            },
            {
                "title": "前置放大与信号处理",
                "summary": "阐述前置放大器、成形放大器及滤波电路在信号提取中的作用。",
                "expandable": True
            },
            {
                "title": "高压与偏置电路",
                "summary": "说明高压电源的设计要求、偏置电路的稳定性和安全防护措施。",
                "expandable": True
            },
            {
                "title": "定时与符合测量",
                "summary": "讲解时间-幅度转换、符合与反符合电路在事件鉴别中的应用。",
                "expandable": True
            },
            {
                "title": "数据采集与处理系统",
                "summary": "介绍DAQ硬件、软件架构以及实时数据处理与存储方法。",
                "expandable": True
            },
            {
                "title": "校准与质量保证",
                "summary": "阐述能量校准、效率校正及质量控制流程，确保测量可靠性。",
                "expandable": True
            }
        ]
    },
    "信号传输与匹配": {
        "root": {
            "title": "信号传输与匹配",
            "summary": "研究核电子系统中信号的有效传输与阻抗匹配技术。"
        },
        "children": [
            {
                "title": "传输线理论",
                "summary": "阐述传输线的分布参数、波动方程及时延特性。",
                "expandable": True
            },
            {
                "title": "阻抗匹配",
                "summary": "介绍源、负载及传输线阻抗匹配的原则与方法。",
                "expandable": True
            },
            {
                "title": "终端与反射",
                "summary": "分析终端阻抗、反射系数及抑制反射的措施。",
                "expandable": True
            },
            {
                "title": "噪声与干扰",
                "summary": "探讨电子噪声、热噪声及串扰的来源与防护。",
                "expandable": True
            },
            {
                "title": "连接器与布线",
                "summary": "讨论连接器选型、布线布局对信号完整性的影响。",
                "expandable": True
            },
            {
                "title": "带宽与信号完整性",
                "summary": "评估系统带宽、频率响应及信号失真与时序问题。",
                "expandable": True
            }
        ]
    },
    "符合测量实验": {
        "root": {
            "title": "符合测量实验",
            "summary": "通过检测两个或多个探测器的同步信号，研究粒子或核事件的时间关联与特性。"
        },
        "children": [
            {
                "title": "原理与概念",
                "summary": "阐述符合测量的基本原理、时间关联概念及符合类型。",
                "expandable": True
            },
            {
                "title": "探测与前端电子学",
                "summary": "介绍探测器工作原理、信号放大与成形电路及其在符合测量中的作用。",
                "expandable": True
            },
            {
                "title": "符合逻辑与时间窗口",
                "summary": "说明符合判定电路、时间窗口设置及随机符合的排除方法。",
                "expandable": True
            },
            {
                "title": "数据采集与处理",
                "summary": "描述触发、数字化存储及符合事件的软件分析流程。",
                "expandable": True
            },
            {
                "title": "误差分析与校正",
                "summary": "分析随机符合、效率校正及系统时间分辨率对测量结果的影响。",
                "expandable": True
            },
            {
                "title": "应用实例",
                "summary": "展示核谱、医学成像等领域中符合测量技术的实际应用案例。",
                "expandable": True
            }
        ]
    },
    "峰康比与本底": {
        "root": {
            "title": "峰康比与本底",
            "summary": "研究核探测信号峰强度相对于本底噪声的比值，以评估仪器灵敏度和测量精度。"
        },
        "children": [
            {
                "title": "本底来源与特性",
                "summary": "阐述宇宙射线、同位素和探测器噪声等本底的主要来源与统计特性。",
                "expandable": True
            },
            {
                "title": "峰康比定义与计算",
                "summary": "给出峰康比的数学定义，说明如何从能谱中提取峰面积与本底计数进行比值计算。",
                "expandable": True
            },
            {
                "title": "影响峰康比的因素",
                "summary": "分析探测器效率、屏蔽效果、计数时间及信号处理等关键因素对峰康比的影响。",
                "expandable": True
            },
            {
                "title": "降低本底的技术与方法",
                "summary": "介绍被动屏蔽、主动反符合、低本底材料及选址优化等降低本底的技术手段。",
                "expandable": True
            },
            {
                "title": "峰康比在核仪器中的应用",
                "summary": "阐述峰康比在谱仪、剂量计和成像系统中用于评估灵敏度和信噪比的作用。",
                "expandable": True
            },
            {
                "title": "峰康比测量与数据处理",
                "summary": "说明峰康比的实验测量步骤，包括能谱采集、峰拟合与本底扣除的数据处理方法。",
                "expandable": True
            }
        ]
    },
    "水文地质基础": {
        "root": {
            "title": "水文地质基础",
            "summary": "研究地下水的形成、分布、运移及其与岩石相互作用的基础学科。"
        },
        "children": [
            {
                "title": "地下水基本概念",
                "summary": "阐述地下水的定义、存在形态及其在自然循环中的地位。",
                "expandable": True
            },
            {
                "title": "含水层与隔水层",
                "summary": "区分含水层、弱透水层和隔水层的岩性、孔隙特征及其水文功能。",
                "expandable": True
            },
            {
                "title": "地下水运动规律",
                "summary": "介绍达西定律、水头概念及地下水在多孔介质中的流动机制。",
                "expandable": True
            },
            {
                "title": "补给与排泄",
                "summary": "解释降水渗入、地表水补给及蒸发、泉流等地下水排泄过程。",
                "expandable": True
            },
            {
                "title": "水文地质参数",
                "summary": "阐述渗透系数、储水系数、有效孔隙度等关键参数的测定与意义。",
                "expandable": True
            },
            {
                "title": "地下水与环境",
                "summary": "探讨地下水与生态系统、土地利用及污染的相互影响。",
                "expandable": True
            }
        ]
    },
    "含水层与隔水层": {
        "root": {
            "title": "含水层与隔水层",
            "summary": "研究地下含水层与隔水层的结构、功能及其在水资源与环境中的作用"
        },
        "children": [
            {
                "title": "概念与分类",
                "summary": "含水层与隔水层的基本定义及其分类体系",
                "expandable": True
            },
            {
                "title": "渗透性与孔隙结构",
                "summary": "孔隙、裂隙等结构对渗透性的控制作用",
                "expandable": True
            },
            {
                "title": "水力特性参数",
                "summary": "渗透系数、传导率、储水系数等关键水力参数",
                "expandable": True
            },
            {
                "title": "垂向分布与界面",
                "summary": "含水层与隔水层在垂向上的相互关系及界面特征",
                "expandable": True
            },
            {
                "title": "地下水流动机制",
                "summary": "达西定律及地下水在含水层中的流动规律",
                "expandable": True
            },
            {
                "title": "实际应用与管理",
                "summary": "在水资源开发、污染防控和工程设计中的应用",
                "expandable": True
            }
        ]
    },
    "地下水流动": {
        "root": {
            "title": "地下水流动",
            "summary": "研究地下水中水的运动规律及其在多孔介质中的传输机制"
        },
        "children": [
            {
                "title": "含水层与渗透性",
                "summary": "说明地下水的存储介质及其渗透特性",
                "expandable": True
            },
            {
                "title": "水头与驱动力",
                "summary": "阐述水头梯度如何驱动地下水运动",
                "expandable": True
            },
            {
                "title": "达西定律",
                "summary": "介绍经验公式描述渗透通量与水头梯度的关系",
                "expandable": True
            },
            {
                "title": "流动方程与模型",
                "summary": "建立描述地下水流动的控制方程和数值模型",
                "expandable": True
            },
            {
                "title": "非均质与各向异性",
                "summary": "探讨地质结构对水流路径和速度的影响",
                "expandable": True
            },
            {
                "title": "实际应用与案例",
                "summary": "展示地下水流动在补给、抽水和污染传输中的实际案例",
                "expandable": True
            }
        ]
    },
    "渗透系数": {
        "root": {
            "title": "渗透系数",
            "summary": "描述多孔介质中水流阻力与流速关系的参数"
        },
        "children": [
            {
                "title": "定义与物理意义",
                "summary": "表征单位水力梯度下的比流量，反映介质渗透能力。",
                "expandable": True
            },
            {
                "title": "影响因素",
                "summary": "包括粒径、孔隙结构、饱和度及流体黏度等。",
                "expandable": True
            },
            {
                "title": "测量方法",
                "summary": "现场抽水试验、注水试验及实验室渗透仪测定。",
                "expandable": True
            },
            {
                "title": "空间变异性",
                "summary": "不同尺度下地层渗透性呈显著非均匀分布。",
                "expandable": True
            },
            {
                "title": "尺度效应",
                "summary": "随测试体积增大，测得渗透系数趋于代表性值。",
                "expandable": True
            },
            {
                "title": "模型应用",
                "summary": "用于地下水流、溶质运移及风险评估模型构建。",
                "expandable": True
            }
        ]
    },
    "包气带特征": {
        "root": {
            "title": "包气带特征",
            "summary": "包气带是地表与潜水之间的非饱和带，其水文地质特征决定水分、气体和污染物的迁移。",
            "expandable": True
        },
        "children": [
            {
                "title": "土壤结构与孔隙",
                "summary": "描述土壤颗粒组成、孔隙结构及其对水分和气体储存与流动的影响。",
                "expandable": True
            },
            {
                "title": "含水势与保水曲线",
                "summary": "阐述土壤水势（基质势）随含水率变化的特征曲线及其物理意义。",
                "expandable": True
            },
            {
                "title": "非饱和水流运动",
                "summary": "说明在非饱和条件下的水流运动规律，包括渗透、重新分配和达西定律。",
                "expandable": True
            },
            {
                "title": "气体传输与通气性",
                "summary": "介绍土壤中气体（如氧气、二氧化碳）的扩散、通气性及其对生物过程的影响。",
                "expandable": True
            },
            {
                "title": "污染物迁移衰减",
                "summary": "阐述污染物在非饱和带中的运移机制，包括吸附、降解和弥散等过程。",
                "expandable": True
            },
            {
                "title": "环境因素影响",
                "summary": "分析气候、植被、土地利用等外部因素对包气带水分和气体动态的调控作用。",
                "expandable": True
            }
        ]
    },
    "抽水试验": {
        "root": {
            "title": "抽水试验",
            "summary": "通过抽水试验获取含水层水力参数，评估地下水流与污染传输。"
        },
        "children": [
            {
                "title": "试验原理",
                "summary": "阐明抽水试验的基本原理与假设。",
                "expandable": True
            },
            {
                "title": "试验设计",
                "summary": "确定抽水井、观测井位置及抽水流量。",
                "expandable": True
            },
            {
                "title": "现场实施",
                "summary": "现场安装抽水设备并实施抽水过程。",
                "expandable": True
            },
            {
                "title": "监测与记录",
                "summary": "实时监测水位、水质及抽水流量变化。",
                "expandable": True
            },
            {
                "title": "数据分析",
                "summary": "对抽水降深数据进行处理与曲线拟合。",
                "expandable": True
            },
            {
                "title": "参数解释",
                "summary": "解释水文参数并构建地下水流模型。",
                "expandable": True
            }
        ]
    },
    "地下水补给与排泄": {
        "root": {
            "title": "地下水补给与排泄",
            "summary": "研究地下水的补给来源、过程、排泄方式及其相互关系与影响因素。"
        },
        "children": [
            {
                "title": "补给来源",
                "summary": "包括降雨入渗、地表水渗透及人工补给等来源。",
                "expandable": True
            },
            {
                "title": "补给过程",
                "summary": "描述水从地表向含水层迁移的物理机制。",
                "expandable": True
            },
            {
                "title": "排泄方式",
                "summary": "指地下水通过泉、井、基流等形式返回地表或被抽取。",
                "expandable": True
            },
            {
                "title": "排泄路径",
                "summary": "阐述地下水排泄至河流、湖泊或海洋的自然路径。",
                "expandable": True
            },
            {
                "title": "影响因子",
                "summary": "涵盖气候、土壤、地质及人类活动等因素的作用。",
                "expandable": True
            },
            {
                "title": "补给-排泄平衡",
                "summary": "分析补给量与排泄量的平衡关系及其可持续性。",
                "expandable": True
            }
        ]
    },
    "污染物运移": {
        "root": {
            "title": "污染物运移",
            "summary": "研究污染物在地下水系统中随水流、扩散及化学反应迁移的过程与规律"
        },
        "children": [
            {
                "title": "污染源特性",
                "summary": "描述污染物的类型、浓度及释放方式",
                "expandable": True
            },
            {
                "title": "迁移机制",
                "summary": "包括对流、弥散、扩散、吸附与化学反应等过程",
                "expandable": True
            },
            {
                "title": "水文地质条件",
                "summary": "描述含水层渗透性、孔隙度及非均质性对迁移的影响",
                "expandable": True
            },
            {
                "title": "水动力驱动",
                "summary": "阐述地下水流速、梯度及水头分布对污染物迁移的控制",
                "expandable": True
            },
            {
                "title": "风险评估与模型",
                "summary": "利用数学模型预测污染物浓度分布并评估危害",
                "expandable": True
            },
            {
                "title": "防控与修复技术",
                "summary": "介绍隔离、抽提、生物降解等控制与修复措施",
                "expandable": True
            }
        ]
    },
    "吸附与降解": {
        "root": {
            "title": "吸附与降解",
            "summary": "研究污染物在含水层中的吸附固定与生物/化学降解行为及其对迁移的影响。"
        },
        "children": [
            {
                "title": "吸附机制",
                "summary": "阐述土壤/含水层介质对污染物的物理化学吸附机理。",
                "expandable": True
            },
            {
                "title": "降解过程",
                "summary": "介绍生物降解、化学降解及光降解在地下水系统中的转化路径。",
                "expandable": True
            },
            {
                "title": "影响因子",
                "summary": "分析pH、温度、氧化还原电位等环境因素对吸附降解的控制作用。",
                "expandable": True
            },
            {
                "title": "场地特性",
                "summary": "阐述地形、岩性、地下水流向等场地条件对过程的影响。",
                "expandable": True
            },
            {
                "title": "监测评估",
                "summary": "介绍现场采样、实验室分析及模型评估吸附降解效果的方法。",
                "expandable": True
            },
            {
                "title": "修复技术",
                "summary": "探讨原位生物刺激、吸附剂注入等技术在场地污染修复中的应用。",
                "expandable": True
            }
        ]
    },
    "污染羽演化": {
        "root": {
            "title": "污染羽演化",
            "summary": "研究地下水中污染羽在源项、水动力、迁移机制等作用下的时空演变规律。"
        },
        "children": [
            {
                "title": "污染源特征",
                "summary": "包括污染物种类、释放速率、持续时间及其空间分布特征。",
                "expandable": True
            },
            {
                "title": "含水层条件",
                "summary": "描述介质渗透性、孔隙度、地下水流向与水力梯度等基本水文地质属性。",
                "expandable": True
            },
            {
                "title": "迁移机制",
                "summary": "涵盖对流、弥散、吸附、生物降解及化学反应等影响羽形态的过程。",
                "expandable": True
            },
            {
                "title": "羽状形态演变",
                "summary": "分析羽的横向扩展、纵向迁移、浓度梯度变化及形态不规则性的形成。",
                "expandable": True
            },
            {
                "title": "时间演化规律",
                "summary": "探讨羽随时间的增长、衰减、稳态或回弹等阶段性变化特征。",
                "expandable": True
            },
            {
                "title": "自然衰减与修复",
                "summary": "评估自然衰减过程及人工修复措施对羽演化的影响与控制效果。",
                "expandable": True
            }
        ]
    },
    "污染源识别": {
        "root": {
            "title": "污染源识别",
            "summary": "通过现场调查、化学指纹、水文分析和模型耦合，确定场地污染来源及其迁移特征。"
        },
        "children": [
            {
                "title": "现场勘查与历史回顾",
                "summary": "通过现场踏勘、档案查阅，重建场地历史使用与潜在排放点。",
                "expandable": True
            },
            {
                "title": "污染物指纹分析",
                "summary": "利用化学成分、同位素或光谱特征辨别污染物来源。",
                "expandable": True
            },
            {
                "title": "水文地质条件解析",
                "summary": "分析含水层、渗透系数等，确定污染物迁移路径与速度。",
                "expandable": True
            },
            {
                "title": "监测网络与数据解释",
                "summary": "布置井孔与采样点，运用统计方法解读浓度分布趋势。",
                "expandable": True
            },
            {
                "title": "源-汇耦合模型",
                "summary": "构建源强与受体响应的数学模型，评估因果关系。",
                "expandable": True
            },
            {
                "title": "不确定性分析与验证",
                "summary": "量化参数不确定性和模型误差，校验识别结果的可靠性。",
                "expandable": True
            }
        ]
    },
    "场地环境调查": {
        "root": {
            "title": "场地环境调查",
            "summary": "对场地进行系统调查，识别污染源、评估风险并指导后续治理。"
        },
        "children": [
            {
                "title": "现场踏勘与历史调查",
                "summary": "通过现场踏勘和资料收集，了解场地历史利用与潜在污染源。",
                "expandable": True
            },
            {
                "title": "土壤与地下水采样设计",
                "summary": "制定科学采样方案，确定采样点、深度、频率及质量控制措施。",
                "expandable": True
            },
            {
                "title": "实验室分析与数据质量",
                "summary": "开展土壤、地下水样品的化学分析，实施质量保证与质量控制。",
                "expandable": True
            },
            {
                "title": "污染羽与迁移特征",
                "summary": "分析污染物在土壤和地下水中的分布形态、迁移路径及影响因素。",
                "expandable": True
            },
            {
                "title": "风险评估与健康影响",
                "summary": "评估场地污染物对人体和生态的风险，确定可接受水平。",
                "expandable": True
            },
            {
                "title": "调查方案与报告编制",
                "summary": "制定调查计划，整理分析结果，撰写符合规范的调查报告。",
                "expandable": True
            }
        ]
    },
    "采样与监测": {
        "root": {
            "title": "采样与监测",
            "summary": "针对场地污染水文地质，系统开展采样与监测，获取可靠环境数据。"
        },
        "children": [
            {
                "title": "采样方案设计",
                "summary": "确定采样目标、地点、频次和深度，构建代表性采样网络。",
                "expandable": True
            },
            {
                "title": "采样技术方法",
                "summary": "包括土壤、地下水、地表水和气体等不同介质的取样技术与设备。",
                "expandable": True
            },
            {
                "title": "现场监测与测量",
                "summary": "使用便携式仪器现场测定pH、溶解氧、电导率等关键参数。",
                "expandable": True
            },
            {
                "title": "样品保存与运输",
                "summary": "规定容器、温度、保存剂及链式监管，确保样品完整性。",
                "expandable": True
            },
            {
                "title": "实验室分析与质量控制",
                "summary": "选择合适分析方法，执行质量控制与样品复核，保证结果可靠。",
                "expandable": True
            },
            {
                "title": "数据处理与解释",
                "summary": "对监测数据进行验证、统计和趋势分析，形成评估报告。",
                "expandable": True
            }
        ]
    },
    "地下水监测井": {
        "root": {
            "title": "地下水监测井",
            "summary": "用于监测地下水水位、水质和污染动态的专用井设施。"
        },
        "children": [
            {
                "title": "井的设计与施工",
                "summary": "包括井位选址、井深、直径、套管及滤料等设计原则与施工流程。",
                "expandable": True
            },
            {
                "title": "井的材料与结构",
                "summary": "阐述井壁、套管、滤网、沉砂管等材料的选用标准及其结构功能。",
                "expandable": True
            },
            {
                "title": "监测设备与仪器",
                "summary": "介绍水位计、水质探头、流量计等监测仪器的工作原理与校准要求。",
                "expandable": True
            },
            {
                "title": "采样方法与技术",
                "summary": "阐述地下水采样前的井口处理、取样深度、抽水速率及样品保存的规范。",
                "expandable": True
            },
            {
                "title": "数据管理与分析",
                "summary": "说明监测数据的记录、存储、质量控制及统计分析方法。",
                "expandable": True
            },
            {
                "title": "法规与标准",
                "summary": "概述国内外地下水监测井相关的法规、指南和技术标准的要点。",
                "expandable": True
            }
        ]
    },
    "水文地质勘察": {
        "root": {
            "title": "水文地质勘察",
            "summary": "系统调查地下水的分布、补给、运移规律，为资源开发与环境保护提供依据。"
        },
        "children": [
            {
                "title": "现场调查与资料收集",
                "summary": "通过现场踏勘、文献检索获取项目背景与已有地质资料。",
                "expandable": True
            },
            {
                "title": "水文地质测绘",
                "summary": "绘制地下水系统空间分布图，标注含水层、隔水层及水力特征。",
                "expandable": True
            },
            {
                "title": "地下水位监测",
                "summary": "在监测井中定时测量水位，分析季节性变化与补给排泄关系。",
                "expandable": True
            },
            {
                "title": "含水层试验",
                "summary": "开展抽水试验与注水试验，获取渗透系数、储水系数等水力参数。",
                "expandable": True
            },
            {
                "title": "采样与实验室分析",
                "summary": "采集地下水和岩土样本，进行化学成分、微生物及同位素分析。",
                "expandable": True
            },
            {
                "title": "数据解释与模型构建",
                "summary": "综合监测与分析结果，构建地下水流模型，评估资源与风险。",
                "expandable": True
            }
        ]
    },
    "水质分析": {
        "root": {
            "title": "水质分析",
            "summary": "对水体中物理、化学、生物指标进行系统检测与评价的技术与方法。"
        },
        "children": [
            {
                "title": "采样与现场检测",
                "summary": "包括采样点布设、取样方法、现场快速检测仪器与质量控制。",
                "expandable": True
            },
            {
                "title": "物理化学参数",
                "summary": "测定水温、pH、溶解氧、电导率等基本水质指标。",
                "expandable": True
            },
            {
                "title": "微生物指标",
                "summary": "检测细菌、大肠杆菌等指示微生物，评估水体卫生安全。",
                "expandable": True
            },
            {
                "title": "有机污染物分析",
                "summary": "利用GC-MS、LC-MS等技术鉴定和定量水体中的有机污染物。",
                "expandable": True
            },
            {
                "title": "金属与无机物检测",
                "summary": "采用ICP-OES、ICP-MS等方法测定重金属和营养盐等无机成分。",
                "expandable": True
            },
            {
                "title": "数据处理与评价",
                "summary": "对实验数据进行统计分析、结果解释并形成水质评价报告。",
                "expandable": True
            }
        ]
    },
    "重金属污染": {
        "root": {
            "title": "重金属污染",
            "summary": "重金属污染指环境中重金属的来源、迁移、形态、毒理、风险与修复等关键环节。"
        },
        "children": [
            {
                "title": "污染来源与类型",
                "summary": "阐述重金属的工业、农业、生活等主要来源及其化学形态分类。",
                "expandable": True
            },
            {
                "title": "迁移转化机制",
                "summary": "说明重金属在土壤、地下水和地表水中的吸附、沉淀、氧化还原等迁移转化过程。",
                "expandable": True
            },
            {
                "title": "形态与生物有效性",
                "summary": "解析不同化学形态对重金属迁移、吸附和生物吸收的影响。",
                "expandable": True
            },
            {
                "title": "环境影响与毒理",
                "summary": "概述重金属对生态系统和人体健康的毒性、累积及致毒机制。",
                "expandable": True
            },
            {
                "title": "风险评估与管理",
                "summary": "介绍重金属在场地中的暴露途径、风险评估模型及防控管理措施。",
                "expandable": True
            },
            {
                "title": "修复技术与工程",
                "summary": "综述物理、化学、生物等重金属去除与固定技术及其工程应用。",
                "expandable": True
            }
        ]
    },
    "有机污染物": {
        "root": {
            "title": "有机污染物",
            "summary": "研究有机污染物在场地水文环境中的来源、迁移、风险与治理。"
        },
        "children": [
            {
                "title": "污染物分类",
                "summary": "按化学结构和用途将有机污染物划分为多类。",
                "expandable": True
            },
            {
                "title": "来源与排放",
                "summary": "阐述工业、农业、生活等主要排放源及其排放特征。",
                "expandable": True
            },
            {
                "title": "迁移转化",
                "summary": "描述有机污染物在土壤、地下水中对流、扩散、吸附与降解过程。",
                "expandable": True
            },
            {
                "title": "吸附降解",
                "summary": "分析有机污染物在介质中的吸附机理及生物、化学降解途径。",
                "expandable": True
            },
            {
                "title": "风险评估",
                "summary": "评估有机污染物对人体健康和生态系统的危害及其暴露途径。",
                "expandable": True
            },
            {
                "title": "监测治理",
                "summary": "介绍现场采样、实验室分析技术以及修复和管控措施。",
                "expandable": True
            }
        ]
    },
    "土壤污染": {
        "root": {
            "title": "土壤污染",
            "summary": "土壤污染指有害物质进入土壤导致环境质量下降和生态风险的现象。"
        },
        "children": [
            {
                "title": "污染来源",
                "summary": "包括工业、农业、生活和自然等多种人类活动和自然过程。",
                "expandable": True
            },
            {
                "title": "污染物类型",
                "summary": "常见有重金属、有机物、农药、石油烃和放射性物质等。",
                "expandable": True
            },
            {
                "title": "迁移转化",
                "summary": "描述污染物在土壤中的吸附、迁移、降解和转化过程。",
                "expandable": True
            },
            {
                "title": "生态与健康影响",
                "summary": "阐述对植物、动物、微生物及人体健康的负面效应。",
                "expandable": True
            },
            {
                "title": "监测评估",
                "summary": "涉及土壤采样、实验室分析和风险评估方法。",
                "expandable": True
            },
            {
                "title": "修复治理",
                "summary": "包括物理、化学、生物及综合修复技术与管理制度。",
                "expandable": True
            }
        ]
    },
    "风险评估": {
        "root": {
            "title": "风险评估",
            "summary": "对场地污染水文地质系统中潜在危害进行识别、量化和管控的系统过程。"
        },
        "children": [
            {
                "title": "污染源辨识",
                "summary": "识别场地内污染物的来源、类型及释放特征。",
                "expandable": True
            },
            {
                "title": "迁移转化分析",
                "summary": "评估污染物在地下水系统中的迁移、吸附与降解过程。",
                "expandable": True
            },
            {
                "title": "暴露评估",
                "summary": "分析人类及生态受体与污染物的接触途径与频率。",
                "expandable": True
            },
            {
                "title": "毒性评估",
                "summary": "评估污染物对人体和生态系统的剂量-反应关系。",
                "expandable": True
            },
            {
                "title": "风险表征",
                "summary": "综合危害、暴露和毒性信息量化风险水平。",
                "expandable": True
            },
            {
                "title": "风险管理",
                "summary": "制定控制措施和监测计划以降低或消除风险。",
                "expandable": True
            }
        ]
    },
    "概念模型": {
        "root": {
            "title": "概念模型",
            "summary": "用于描述场地污染水文地质系统中关键要素及其相互关系的概念框架。"
        },
        "children": [
            {
                "title": "定义与目的",
                "summary": "明确概念模型的目标与适用场景，指导后续工作。",
                "expandable": True
            },
            {
                "title": "组成要素",
                "summary": "阐述地质、水文、污染源、迁移路径、受体等核心组成。",
                "expandable": True
            },
            {
                "title": "数据需求",
                "summary": "说明建模所需的地质、水化学、监测等数据及其质量要求。",
                "expandable": True
            },
            {
                "title": "建模流程",
                "summary": "概述从概念构思、资料收集到模型绘制的步骤流程。",
                "expandable": True
            },
            {
                "title": "不确定性与简化",
                "summary": "分析模型简化、假设及不确定性来源，评估可靠性。",
                "expandable": True
            },
            {
                "title": "应用场景",
                "summary": "展示在风险评估、修复设计、监管决策中的实际应用。",
                "expandable": True
            }
        ]
    },
    "数值模拟": {
        "root": {
            "title": "数值模拟",
            "summary": "通过数学模型和计算方法，模拟地下水流和污染物运移，评估场地污染风险与修复方案。"
        },
        "children": [
            {
                "title": "基本概念与建模流程",
                "summary": "从问题定义到模型构建的完整步骤与关键要素。",
                "expandable": True
            },
            {
                "title": "控制方程与物理过程",
                "summary": "描述地下水流动和污染物迁移的基本微分方程及假设。",
                "expandable": True
            },
            {
                "title": "离散化方法",
                "summary": "将连续方程转化为网格或单元的代数方程的技术。",
                "expandable": True
            },
            {
                "title": "边界条件与源汇",
                "summary": "定义模型域外的水力与化学边界以及污染源和汇项。",
                "expandable": True
            },
            {
                "title": "求解算法与数值稳定性",
                "summary": "选择合适的时间步进和迭代求解方法，确保数值结果收敛。",
                "expandable": True
            },
            {
                "title": "模型校验与不确定性",
                "summary": "通过实测数据校正模型参数并评估模拟结果的可靠性和不确定性。",
                "expandable": True
            }
        ]
    },
    "抽出处理": {
        "root": {
            "title": "抽出处理",
            "summary": "针对地下水污染的抽出处理技术概述"
        },
        "children": [
            {
                "title": "基本原理",
                "summary": "通过抽提地下水并进行处理以降低污染物浓度的技术原理",
                "expandable": True
            },
            {
                "title": "场地调查",
                "summary": "评估含水层特性、污染范围及水文地质条件，为设计提供依据",
                "expandable": True
            },
            {
                "title": "抽提系统设计",
                "summary": "确定抽提井数量、深度、抽提速率及布局，确保有效捕获污染羽",
                "expandable": True
            },
            {
                "title": "废水处理工艺",
                "summary": "采用物理、化学或生物方法对抽提出的污染水进行净化处理",
                "expandable": True
            },
            {
                "title": "运行与监测",
                "summary": "实时监控抽提量、水位、污染物浓度并调整操作参数",
                "expandable": True
            },
            {
                "title": "经济与环境影响评估",
                "summary": "分析投资、运行成本及对周边环境的潜在影响，指导决策",
                "expandable": True
            }
        ]
    },
    "原位化学氧化": {
        "root": {
            "title": "原位化学氧化",
            "summary": "原位化学氧化是一种通过注入氧化剂在原地降解土壤和地下水中有机污染物的修复技术。"
        },
        "children": [
            {
                "title": "氧化剂种类与特性",
                "summary": "常用氧化剂如高锰酸钾、过氧化氢、臭氧等，各具氧化能力和适用条件。",
                "expandable": True
            },
            {
                "title": "注入与混合技术",
                "summary": "通过注射井、渗透井或混合设备实现氧化剂在污染区的均匀分布。",
                "expandable": True
            },
            {
                "title": "污染物降解机理",
                "summary": "氧化剂产生自由基或直接氧化有机分子，实现污染物的化学转化与矿化。",
                "expandable": True
            },
            {
                "title": "场地评估与设计",
                "summary": "通过土壤渗透性、氧化剂需求量和反应动力学确定注入方案和监测点。",
                "expandable": True
            },
            {
                "title": "性能监测与评估",
                "summary": "实时监测氧化剂浓度、产物生成及污染物残余量，评估修复效果。",
                "expandable": True
            },
            {
                "title": "环境与安全考量",
                "summary": "注意氧化剂对非目标物质的影响、副产物风险及现场作业安全。",
                "expandable": True
            }
        ]
    },
    "可渗透反应墙": {
        "root": {
            "title": "可渗透反应墙",
            "summary": "可渗透反应墙是一种在地下水路径上设置活性介质墙体，以原位去除污染物的技术。"
        },
        "children": [
            {
                "title": "概念与原理",
                "summary": "通过在地下水流动路径上设置活性墙体，拦截并降解污染物。",
                "expandable": True
            },
            {
                "title": "设计参数",
                "summary": "确定墙体厚度、深度、孔隙率及介质配比等关键参数。",
                "expandable": True
            },
            {
                "title": "反应介质材料",
                "summary": "选用活性炭、铁系材料、零价铁等实现吸附、沉淀或生物降解。",
                "expandable": True
            },
            {
                "title": "施工与安装",
                "summary": "包括现场钻孔、墙体铺设、介质填充及后期密封等工序。",
                "expandable": True
            },
            {
                "title": "监测与评估",
                "summary": "通过水位、浓度和介质寿命等指标实时监测墙体性能。",
                "expandable": True
            },
            {
                "title": "环境影响与管理",
                "summary": "评估墙体对地下水流场和生态的影响，制定长期管理方案。",
                "expandable": True
            }
        ]
    },
    "监测自然衰减": {
        "root": {
            "title": "监测自然衰减",
            "summary": "通过系统监测与评估自然衰减过程，验证污染场地恢复效果与风险可控性。"
        },
        "children": [
            {
                "title": "场地概念模型",
                "summary": "构建场地地质、水文与污染特征的整体框架。",
                "expandable": True
            },
            {
                "title": "自然衰减机理",
                "summary": "阐明生物降解、吸附、稀释等降低污染的机制。",
                "expandable": True
            },
            {
                "title": "监测指标选取",
                "summary": "选择电子受体、降解产物等关键参数进行监测。",
                "expandable": True
            },
            {
                "title": "监测网络设计",
                "summary": "确定采样井位置、深度与频率，实现时空覆盖。",
                "expandable": True
            },
            {
                "title": "数据解释评估",
                "summary": "分析监测数据趋势，评估衰减效果与风险。",
                "expandable": True
            },
            {
                "title": "监管与管理",
                "summary": "依据标准制定监测计划与后续管理措施。",
                "expandable": True
            }
        ]
    },
    "阻隔与帷幕": {
        "root": {
            "title": "阻隔与帷幕",
            "summary": "通过物理、水力或化学手段在场地污染水文地质中设置阻隔与帷幕，控制污染物迁移"
        },
        "children": [
            {
                "title": "阻隔原理",
                "summary": "阐述阻隔技术的基本原理，包括物理、水力和化学屏障作用",
                "expandable": True
            },
            {
                "title": "帷幕结构",
                "summary": "介绍帷幕墙体的类型、材料及适用条件",
                "expandable": True
            },
            {
                "title": "设计方法",
                "summary": "阐述阻隔与帷幕的设计流程、参数选取及安全评估",
                "expandable": True
            },
            {
                "title": "施工技术",
                "summary": "说明阻隔与帷幕的施工工艺、设备及质量控制要点",
                "expandable": True
            },
            {
                "title": "监测评估",
                "summary": "介绍监测手段、指标体系及阻隔帷幕效果评价方法",
                "expandable": True
            },
            {
                "title": "案例实践",
                "summary": "通过实际工程案例展示阻隔帷幕的应用与效果",
                "expandable": True
            }
        ]
    },
    "修复效果评估": {
        "root": {
            "title": "修复效果评估",
            "summary": "评估修复措施在场地污染水文地质中的实际效果与可持续性"
        },
        "children": [
            {
                "title": "监测指标体系",
                "summary": "确定评估所需的关键水质、土壤和气体指标",
                "expandable": True
            },
            {
                "title": "监测技术方法",
                "summary": "现场采样、在线监测与遥感等技术的应用",
                "expandable": True
            },
            {
                "title": "效果评价模型",
                "summary": "利用数学模型量化污染物去除率和恢复程度",
                "expandable": True
            },
            {
                "title": "风险健康评估",
                "summary": "评估修复后对生态和人体健康的潜在风险",
                "expandable": True
            },
            {
                "title": "经济可行性评估",
                "summary": "比较修复成本与长期环境收益的性价比",
                "expandable": True
            },
            {
                "title": "长期跟踪评估",
                "summary": "持续监测修复效果并评估长期可持续性",
                "expandable": True
            }
        ]
    }
}
