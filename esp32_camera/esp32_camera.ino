#include <esp_camera.h>
#include <WiFi.h>
#include <HTTPClient.h>

// Configure these locally before flashing. Do not commit real values.
const char *WIFI_SSID = "Kalisha";
const char *WIFI_PASSWORD = "26022021";
const char *INGEST_URL = "http://10.136.118.145:8000/dashboard/attendance/camera/ingest/";
const char *CAMERA_KEY = "esp32_camera_key_2024_smartfuel_secure_v1";
const char *CAMERA_ID = "esp32-camera";

void setupCamera();

void setup() {
  Serial.begin(115200);
  setupCamera();
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\nCamera connected");
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    WiFi.reconnect();
    delay(1000);
    return;
  }

  camera_fb_t *frame = esp_camera_fb_get();
  if (!frame) {
    Serial.println("Camera capture failed");
    delay(1000);
    return;
  }

  HTTPClient http;
  http.begin(INGEST_URL);
  http.addHeader("Content-Type", "image/jpeg");
  http.addHeader("X-Camera-Key", CAMERA_KEY);
  http.addHeader("X-Camera-ID", CAMERA_ID);
  int status = http.POST(frame->buf, frame->len);
  Serial.printf("Frame upload status: %d\n", status);
  http.end();
  esp_camera_fb_return(frame);
  delay(250); // approximately 4 frames per second
}

void setupCamera() {
  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = 5;
  config.pin_d1 = 18;
  config.pin_d2 = 19;
  config.pin_d3 = 21;
  config.pin_d4 = 36;
  config.pin_d5 = 39;
  config.pin_d6 = 34;
  config.pin_d7 = 35;
  config.pin_xclk = 0;
  config.pin_pclk = 22;
  config.pin_vsync = 25;
  config.pin_href = 23;
  config.pin_sscb_sda = 26;
  config.pin_sscb_scl = 27;
  config.pin_pwdn = 32;
  config.pin_reset = -1;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  config.frame_size = FRAMESIZE_VGA;
  config.jpeg_quality = 12;
  config.fb_count = 1;

  if (esp_camera_init(&config) != ESP_OK) {
    Serial.println("Camera init failed");
    while (true) {
      delay(1000);
    }
  }
}
