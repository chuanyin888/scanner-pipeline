/*
 * 高拍仪工作台状态灯（ESP32-S3 板载 WS2812, GPIO48）
 *
 * 串口(115200) 单行指令：
 *   IDLE  -> 红灯常亮      待命：空台，可以放设备
 *   BUSY  -> 黄白交替      检测到设备，等稳定/对焦中
 *   OK    -> 绿灯闪        拍照完成，闪到把设备拿走为止
 *   WARN  -> 紫灯闪        拍了但可能失焦，建议重拍
 *   ERROR -> 红白快闪      异常：相机掉线 / 写盘失败
 *   BOOT  -> 蓝灯呼吸      启动/标定中
 *   REGION-> 红绿蓝循环 5 秒 + IO2 常亮后回待命（区域卡切换成功）
 *   CANCEL-> 白灯闪 3 下后回待命（取消区域卡）
 *
 * 关键点：WS2812 连续高速刷帧会不刷新（表现为"灯卡在第一种颜色"），
 * 所以这里只在颜色真正变化时写一次，并且各效果都用离散色阶（≤7 次/秒）。
 * 每次写灯都会在串口打印一行 "# rgb=..." 便于核对。
 */
#include <Arduino.h>

static const int LED_PIN = 48;
static const int IO2_PIN = 2;  // 丝印 IO2 的那颗灯

static uint8_t curR = 255, curG = 255, curB = 255;
static bool curValid = false;

static void ws(uint8_t r, uint8_t g, uint8_t b) {
    if (curValid && r == curR && g == curG && b == curB) return;  // 颜色没变就不写
    curR = r; curG = g; curB = b; curValid = true;
    neopixelWrite(LED_PIN, r, g, b);
    Serial.printf("# rgb=%d,%d,%d t=%lu\n", r, g, b, (unsigned long)millis());
}

enum Mode { M_IDLE, M_BUSY, M_OK, M_WARN, M_ERROR, M_BOOT, M_REGION, M_CANCEL };
static Mode mode = M_BOOT;
static uint32_t t0 = 0;
static bool io2_on = false;

static void setMode(Mode m) {
    if (mode == M_REGION && m != M_REGION) {  // 离开切换确认态时把 IO2 关掉
        pinMode(IO2_PIN, OUTPUT);
        digitalWrite(IO2_PIN, LOW);
        io2_on = false;
    }
    mode = m;
    t0 = millis();
}

static void handleLine(String s) {
    s.trim();
    s.toUpperCase();
    if (s == "IDLE") setMode(M_IDLE);
    else if (s == "BUSY") setMode(M_BUSY);
    else if (s == "OK") setMode(M_OK);
    else if (s == "WARN") setMode(M_WARN);
    else if (s == "ERROR") setMode(M_ERROR);
    else if (s == "BOOT") setMode(M_BOOT);
    else if (s == "REGION") setMode(M_REGION);
    else if (s == "CANCEL") setMode(M_CANCEL);
}

void setup() {
    Serial.begin(115200);
    delay(500);
    Serial.println("status-led ready (GPIO48)");
    setMode(M_IDLE);
}

void loop() {
    while (Serial.available()) {
        handleLine(Serial.readStringUntil('\n'));
    }

    uint32_t t = millis() - t0;
    switch (mode) {
        case M_IDLE:
            ws(255, 0, 0);  // 红常亮
            break;

        case M_BUSY:  // 黄 <-> 白，各 250ms
            if ((t / 250) % 2 == 0) ws(255, 180, 0);
            else ws(255, 255, 255);
            break;

        case M_OK:  // 绿闪：亮 300ms / 灭 300ms
            if ((t % 600) < 300) ws(0, 255, 0);
            else ws(0, 0, 0);
            break;

        case M_WARN:  // 紫闪：亮 350ms / 灭 350ms
            if ((t % 700) < 350) ws(170, 0, 255);
            else ws(0, 0, 0);
            break;

        case M_ERROR:  // 红白交替，各 200ms
            if ((t / 200) % 2 == 0) ws(255, 0, 0);
            else ws(255, 255, 255);
            break;

        case M_BOOT: {  // 蓝呼吸：12 个色阶，每阶 150ms
            static const uint8_t LV[6] = {20, 60, 120, 180, 220, 255};
            uint8_t idx = (t / 150) % 12;
            uint8_t level = LV[idx < 6 ? idx : 11 - idx];
            ws(0, 0, level);
            break;
        }

        case M_REGION: {  // 区域卡切换成功：红绿蓝循环 5 秒 + IO2 常亮，然后回待命红灯
            if (!io2_on) {
                pinMode(IO2_PIN, OUTPUT);
                digitalWrite(IO2_PIN, HIGH);  // 若这颗灯是低电平点亮，实测后改这里
                io2_on = true;
                Serial.println("# io2=on (5s)");
            }
            // 每色 1.66 秒，5 秒正好走完红->绿->蓝，不会在结尾多闪一次红
            uint8_t phase = (t / 1660) % 3;
            if (phase == 0) ws(255, 0, 0);
            else if (phase == 1) ws(0, 255, 0);
            else ws(0, 0, 255);
            if (t >= 5000) {
                digitalWrite(IO2_PIN, LOW);
                io2_on = false;
                Serial.println("# io2=off");
                setMode(M_IDLE);
            }
            break;
        }

        case M_CANCEL: {  // 取消区域卡：白灯闪 3 下（亮 220ms / 灭 220ms），然后回待命
            uint32_t cycle = t % 440;
            if (t / 440 < 3) {
                if (cycle < 220) ws(255, 255, 255);
                else ws(0, 0, 0);
            } else {
                setMode(M_IDLE);
            }
            break;
        }
    }
    delay(12);
}
