# Description: WyzeSense Bridge to MQTT
#!/usr/bin/env python3

import paho.mqtt.client as mqtt # pip3 install paho-mqtt
import paho.mqtt.publish as publish
import paho.mqtt.subscribe as subscribe
import logging
import asyncio
import os
import sys
import json
import time
import yaml # pip3 install pyyaml
import usb.core # pip3 install pyusb
import requests
from datetime import datetime
from enum import Enum
import struct

class WyzePacketType(Enum):
    Ping = 0x01
    Pong = 0x02
    Scan = 0x03
    Data = 0x04
    KeyExchange = 0x05
    Unknown = 0xFF

class WyzePacket:
    def __init__(self, packet_type, payload=None):
        self.type = packet_type
        self.payload = payload

    def to_bytes(self):
        packet_type_byte = struct.pack('B', self.type.value)
        if self.payload is not None:
            payload_bytes = struct.pack('B' * len(self.payload), *self.payload)
            return packet_type_byte + payload_bytes
        else:
            return packet_type_byte

    @staticmethod
    def from_bytes(bytes):
        packet_type = WyzePacketType(struct.unpack('B', bytes[0])[0])
        if len(bytes) > 1:
            payload = list(struct.unpack('B' * (len(bytes) - 1), bytes[1:]))
            return WyzePacket(packet_type, payload)
        else:
            return WyzePacket(packet_type)

# Load configuration
with open('config.yaml', 'r') as f:
    config = yaml.safe_load(f)

# MQTT Client
client = mqtt.Client()

# Logger
logging.basicConfig(level=config['logging']['level'])
logger = logging.getLogger(__name__)


# WyzeSense Dongle
class WyzeDongle:
    
    def __init__(self, serial_port, baud_rate=115200, timeout=1):
        self.serial_port = serial_port
        self.logger = logging.getLogger(__name__)

    async def start(self):
        self.logger.info("Starting Wyze dongle...")
        await self.serial_port.open()
        self.logger.info("Wyze dongle started.")

        while self.serial_port.is_open:
            try:
                packet = await self.serial_port.read_packet()
                if packet is not None:
                    self.logger.debug(f"Received packet: {packet}")
                    await self.handle_packet(packet)
            except Exception as ex:
                self.logger.error(f"Error reading from serial port: {ex}")

    def handle_packet(self, packet):
        if packet is None:
            return

        if packet.Type == WyzePacketType.SensorEvent:
            self.handle_sensor_event(packet)
        elif packet.Type == WyzePacketType.SensorDiscovery:
            self.handle_sensor_discovery(packet)
        elif packet.Type == WyzePacketType.SensorDeletion:
            self.handle_sensor_deletion(packet)
        elif packet.Type == WyzePacketType.SensorUpdate:
            self.handle_sensor_update(packet)
        elif packet.Type == WyzePacketType.DongleInformation:
            self.handle_dongle_information(packet)
        else:
            print(f"Unhandled packet type: {packet.Type}")

    def handle_sensor_event(self, packet):
        mac = ''.join(['%02X' % b for b in packet[5:13]])
        event_type = packet[13]
        event_state = packet[14]
        battery = packet[15]
        signal = packet[16]
        timestamp = datetime.datetime.utcfromtimestamp(struct.unpack('I', packet[17:21])[0])

        if event_type == 0x01:
            # Contact Sensor
            self.sensor_event(mac, "contact", "open" if event_state == 0x01 else "close", battery, signal, timestamp)
        elif event_type == 0x02:
            # Motion Sensor
            self.sensor_event(mac, "motion", "active" if event_state == 0x01 else "inactive", battery, signal, timestamp)
        elif event_type == 0x03:
            # Button Pressed
            self.sensor_event(mac, "button", "pressed", battery, signal, timestamp)

    def sensor_event(self, mac, sensor_type, state, battery, signal, timestamp):
        print(f"SensorEvent: mac={mac}, type={sensor_type}, state={state}, battery={battery}, signal={signal}, timestamp={timestamp}")

    def handle_key_exchange_packet(self, packet):
        self._key = packet.payload
        self.send_packet(WyzePacket(WyzePacketType.KeyExchangeResponse, self._key))

    def process_usb(data):
        if len(data) > 0:
            cmd = data[0]
            length = data[1]
            payload = data[2:2+length]
            chk = data[2+length] if len(data) > 2+length else None

            calc_chk = calculate_checksum(cmd, length, payload)

            if chk == calc_chk:
                process_command(cmd, payload)
            else:
                logger.error(f"Checksum error in ProcessUSB. Received {chk}, calculated {calc_chk}.")
        else:
            logger.error("No data received in ProcessUSB.")

        def calculate_checksum(data, start, length):
            num = 0
            for i in range(start, start + length):
                num += data[i]
            return num

        def process_command(command, payload=None):
            array = [0] * (10 + (len(payload) if payload else 0))
            array[0] = 255
            array[1] = 255
            array[2] = 53
            array[3] = 1
            array[4] = len(payload) if payload else 0
            array[5] = command
            if payload:
                array[6:6+len(payload)] = payload
            array[-4] = calculate_checksum(array, 2, len(array) - 6)
            array[-3] = 171
            array[-2] = 171
            array[-1] = 171
            return array

    def data_received(self, data):
        # Parse the received data
        packet = self.parse_packet(data)

        # Check if the packet is valid
        if packet is None:
            return

        # Handle the packet based on its type
        if packet.type == WyzePacketType.PING_RESP:
            self.handle_ping_resp(packet)
        elif packet.type == WyzePacketType.DEVICE_LIST_RESP:
            self.handle_device_list_resp(packet)
        elif packet.type == WyzePacketType.DEVICE_STATE_RESP:
            self.handle_device_state_resp(packet)
        elif packet.type == WyzePacketType.DEVICE_ADD_RESP:
            self.handle_device_add_resp(packet)
        elif packet.type == WyzePacketType.DEVICE_DELETE_RESP:
            self.handle_device_delete_resp(packet)
        elif packet.type == WyzePacketType.DEVICE_INFO_RESP:
            self.handle_device_info_resp(packet)
        elif packet.type == WyzePacketType.DEVICE_EVENT:
            self.handle_device_event(packet)
        else:
            print(f"Unhandled packet type: {packet.type}")

    def write_command(self, command_type, payload):
        # Create the header
        header = struct.pack("<BBH", 0xCC, command_type, len(payload))
        # Create the footer
        footer = struct.pack("<H", self.calculate_checksum(header + payload))
        # Combine header, payload, and footer
        command = header + payload + footer
        # Write the command to the dongle
        self.write_to_dongle(command)

    def refresh_sensors(self):
        # Update the list of sensors
        pass

    def handle_cmd(self, data):
        CMD_SYNC = 0x30
        CMD_QUERY = 0x10
        CMD_JOIN_RESP = 0x34
        CMD_SET = 0x1C

        cmd = data[0]
        len = data[1]
        payload = data[2:len+2]
        chksum = data[len + 2]

        if chksum != self.calc_chksum(data, len + 2):
            print("Checksum error in handle_cmd")
            return

        if cmd == CMD_SYNC:
            print("Got sync packet")
            return

        if cmd == CMD_QUERY:
            print("Got query packet")
            return

        if cmd == CMD_SET:
            print("Got set packet")
            return

        if cmd == CMD_JOIN_RESP:
            print("Got join response packet")
            return

        print("Unknown command: " + str(cmd))

        def handle_resp(self, data):
            cmd = data[0]
            len = data[1]
            payload = data[2:len+2]
            chksum = data[len + 2]

            if chksum != self.calc_chksum(data, len + 2):
                print("Checksum error in handle_resp")
                return

            if cmd == CMD_SYNC:
                print("Got sync response")
                return

            if cmd == CMD_QUERY:
                print("Got query response")
                return

            if cmd == CMD_SET:
                print("Got set response")
                return

        print("Unknown response: " + str(cmd))

    def handle_alarm(self, data):
        mac = data[0:8]
        type = data[8]
        state = data[9]
        battery = data[10]
        signal = data[11]
        timestamp = data[12:16]

        print(f"Alarm: mac={mac}, type={type}, state={state}, battery={battery}, signal={signal}, timestamp={timestamp}")


# MQTT Client Service
class MqttClientService:
    def __init__(self, client):
        self.client = client

    def connect(self, host, port, username, password):
        self.client.username_pw_set(username, password)
        self.client.connect(host, port)

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.client.publish(topic, payload, qos, retain)

    def subscribe(self, topic, qos=0):
        self.client.subscribe(topic, qos)

    def disconnect(self):
        self.client.disconnect()


# Main Program
if __name__ == '__main__':
    # Initialize MQTT client and WyzeSense dongle
    mqtt_service = MqttClientService(client)
    wyze_dongle = WyzeDongle(config['wyze_dongle']['path'])

    # Connect to MQTT broker
    mqtt_service.connect(config['mqtt']['host'], config['mqtt']['port'], config['mqtt']['username'], config['mqtt']['password'])

    # Start WyzeSense dongle
    asyncio.run(wyze_dongle.start())

    # Main loop
    while True:
        # Process data from WyzeSense dongle
        asyncio.run(wyze_dongle.process_usb())

        # Publish sensor data to MQTT broker
        # mqtt_service.publish(config['mqtt']['topic'], 'payload')

        time.sleep(1)
