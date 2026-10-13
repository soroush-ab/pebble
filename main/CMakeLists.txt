idf_component_register(
    SRCS "gimbal.c" "main.c"
         "net_link.c"
         "mic_adc.c"
         "mic_i2s.c"
         "disp.c"
         "frames.c"
         "rfid.c"
         "encoder.c"
         "motion.c"
         "gimbal.c"
         "selftest.c"
    INCLUDE_DIRS "."
    REQUIRES esp_adc esp_driver_spi esp_driver_i2c esp_driver_i2s
             esp_driver_gpio esp_driver_ledc esp_driver_uart
             esp_driver_usb_serial_jtag esp_wifi esp_netif nvs_flash
             esp_timer lwip)
