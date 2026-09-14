#!/usr/bin/with-contenv bashio

echo "Starting Porfiriy Voice Assistant Backend v0.0.59..."

# Export Home Assistant MQTT service credentials if available
if bashio::services.available "mqtt"; then
    echo "Home Assistant MQTT service discovered via bashio."
    export HA_MQTT_HOST=$(bashio::services "mqtt" "host")
    export HA_MQTT_PORT=$(bashio::services "mqtt" "port")
    export HA_MQTT_USERNAME=$(bashio::services "mqtt" "username")
    export HA_MQTT_PASSWORD=$(bashio::services "mqtt" "password")
fi

# Run the python backend
python3 /backend/main.py
