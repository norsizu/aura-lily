# Aura 世界场景素材规范

线上世界模型当前需要 11 个视觉场景。源图统一使用 4:3 PNG，建议 800x600 或更大；转换脚本会缩放为设备使用的 400x300 灰度图。

## 通用构图

- 画面中部偏左保持简洁。Aura 会叠加在画面中央并左移 25 px，人物源区域最大约 220x320。
- 最能识别场景的物体放在画面边缘或纵深处，不要被 Aura 的脸和身体挡住。
- 使用明确的大形状、拉开的灰阶和克制的纹理。细密花纹、小字招牌、微弱渐变和低对比氛围不适合 1-bit 反射屏。
- 不要出现其他人物、文字、Logo、对话框或第二个前景主体。
- 全套保持相近的镜头高度、画风和光线逻辑，让切换场景时仍像同一个连续世界。

## 所需文件

| 文件 | 世界模型位置 | 必须包含的辨识元素 |
| --- | --- | --- |
| `scene_home_living_room.png` | `home.living_room` | 日常休息的客厅；沙发或矮桌、窗户、少量生活物件。 |
| `scene_home_study.png` | `home.study` | 桌沿、台灯、书本或文具，配墙架；这是世界模型中的独立书房。 |
| `scene_home_bedroom.png` | `home.bedroom` | 床沿、床头灯、窗帘或小柜，安静但不要做成过暗的夜景。 |
| `scene_home_kitchen.png` | `home.kitchen` | 操作台、水壶、杯子或简洁橱柜，能覆盖早饭、午饭和晚饭活动。 |
| `scene_home_balcony.png` | `home.balcony` | 栏杆或大窗、室外光线、少量绿植，体现感受天气和温度的地方。 |
| `scene_outside_neighborhood.png` | `outside.neighborhood` | 住处附近的街区；步道、树、长椅、远处店面。 |
| `scene_outside_cafe.png` | `outside.cafe` | 附近咖啡店；桌沿、杯子、柜台或窗边座位，表达安静停留。 |
| `scene_outside_shop.png` | `outside.shop` | 便利店或社区小店内部；成块货架、购物篮或冷柜，不出现可读品牌文字。 |
| `scene_outside_park.png` | `outside.park` | 附近公园；步道、草地、树木或长椅。 |
| `scene_outside_mall.png` | `outside.mall` | 商场内部；开阔走道、扶梯或成块店面，不出现可读品牌文字。 |
| `scene_outside_riverside.png` | `outside.riverside` | 江边或河岸；水面、步道、栏杆或远处岸线，适合散步和短暂停留。 |

服装资源共 9 套：睡衣、洋装、睡裙、休闲装、职业装、冬装、旗袍、马面裙和汉服。职业装替换旧的 `casual_b` 槽位，设备索引保持兼容；`qipao.bin`、`mamian.bin` 和 `hanfu.bin` 均为完整的 600x900 2-bit atlas。

中文同名源图可直接运行：`python3 tools/convert_assets.py --scene-dir /path/to/场景图 --scenes-only`。

把图片放入转换脚本配置的源素材目录后，运行 `python3 firmware/esp32/tools/convert_assets.py`。新场景图未齐时，固件会安全回退到现有家中背景，不会显示空白。
