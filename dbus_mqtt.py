#!/usr/bin/python3 -u
# -*- coding: utf-8 -*-
import argparse
import dbus
import json
import logging
import os
import sys
from time import time
import traceback
import signal
import six
from dbus.mainloop.glib import DBusGMainLoop
from lxml import etree
from collections import OrderedDict
from gi.repository import GLib


# Victron packages
AppDir = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(1, os.path.join(AppDir, 'ext', 'velib_python'))
from logger import setup_logging
from ve_utils import get_vrm_portal_id, exit_on_error, wrap_dbus_value, unwrap_dbus_value
from mqtt_gobject_bridge import MqttGObjectBridge
from mosquitto_bridge_registrator import MosquittoBridgeRegistrator


SoftwareVersion = '1.24'
ServicePrefix = 'com.victronenergy.'
VeDbusInvalid = dbus.Array([], signature=dbus.Signature('i'), variant_level=1)
blocked_items = {('vebus', u'/Interfaces/Mk2/Tunnel'), ('paygo', '/LVD/Threshold')}


class DbusMqtt(MqttGObjectBridge):
	def __init__(self, mqtt_server=None, ca_cert=None, user=None, passwd=None, dbus_address=None,
				init_broker=False, debug=False):
		self._dbus_address = dbus_address
		self._dbus_conn = (dbus.SessionBus() if 'DBUS_SESSION_BUS_ADDRESS' in os.environ else dbus.SystemBus()) \
			if dbus_address is None \
			else dbus.bus.BusConnection(dbus_address)
		self._dbus_conn.add_signal_receiver(self._dbus_name_owner_changed, signal_name='NameOwnerChanged')
		self._connected_to_cloud = False

		# @todo EV Get portal ID from com.victronenergy.system?
		self._system_id = get_vrm_portal_id()
		# Key: D-BUS Service + path, value: topic
		self._topics = {}
		# Key: topic, value: last value seen on D-Bus
		self._values = {}
		# Key: service_type/device_instance, value: D-Bus service name
		self._services = {}
		# Key: short D-Bus service name (eg. 1:31), value: full D-Bus service name (eg. com.victronenergy.settings)
		self._service_ids = {}
		# A queue of value changes, so that we may rate-limit this somewhat
		self.queue = OrderedDict()
		GLib.timeout_add(1000, self._timer_service_queue)
		self._last_queue_run = 0

		if init_broker:
			self._registrator = MosquittoBridgeRegistrator(self._system_id)
			self._registrator.register()
		else:
			self._registrator = None

		self._dbus_conn.add_signal_receiver(self._on_dbus_value_changed,
			dbus_interface='com.victronenergy.BusItem', signal_name='PropertiesChanged', path_keyword='path',
			sender_keyword='service_id')
		services = self._dbus_conn.list_names()
		for service in services:
			if service.startswith('com.victronenergy.'):
				self._service_ids[self._dbus_conn.get_name_owner(service)] = service
				self._scan_dbus_service(service)

		MqttGObjectBridge.__init__(self, mqtt_server, "ve-dbus-mqtt-py", ca_cert, user, passwd, debug)

	def _publish(self, topic, value):
		if self._socket_watch is None:
			return

		# Put it into the queue
		self.queue[topic] = json.dumps(dict(value=value))

	def _unpublish(self, topic):
		if self._socket_watch is None:
			return

		# Never unpublish system serial
		if topic.endswith('/system/0/Serial'):
			return

		# Put it into the queue
		self.queue[topic] = None

	def _publish_all(self):
		for topic in sorted(self._values.keys()):
			value = self._values[topic]
			self._publish(topic, value)

	def _unpublish_all(self):
		for topic in self._values.iterkeys():
			self._unpublish(topic)

	def _on_connect(self, client, userdata, dict, rc):
		MqttGObjectBridge._on_connect(self, client, userdata, dict, rc)
		logging.info('[Connected] Result code {}'.format(rc))
		self._client.subscribe('R/{}/#'.format(self._system_id), 0)
		self._client.subscribe('W/{}/#'.format(self._system_id), 0)
		if self._registrator is not None and self._registrator.client_id is not None:
			self._client.subscribe('$SYS/broker/connection/{}/state'.format(self._registrator.client_id), 0)
		# Send all values at once, because values may have changed when we were disconnected.
		self._publish_all()

	def _on_message(self, client, userdata, msg):
		MqttGObjectBridge._on_message(self, client, userdata, msg)
		if msg.topic.startswith('$SYS/broker/connection/'):
			if int(msg.payload) == 1:
				logging.info('[Message] Connected to cloud broker')
				self._connected_to_cloud = True
			elif self._connected_to_cloud:
				# As long as we have connection with the cloud server, we do not have to worry about
				# authentication. After connection loss, we have to authenticate again, which is a nice
				# moment to initialize the broker again in case our remote_password has been reset on the
				# server, or if someone has unlinks VRM page.
				logging.error('[Message] Lost connection with cloud broker')
				self._connected_to_cloud = False
				self._registrator.register()
			return
		try:
			logging.debug('[Request] {}: {}'.format(msg.topic, str(msg.payload)))
			action, system_id, path = msg.topic.split('/', 2)
			if system_id != self._system_id:
				raise Exception('Unknown system id')
			topic = 'N/{}/{}'.format(system_id, path)
			if action == 'W':
				self._handle_write(topic, msg.payload)
			elif action == 'R':
				self._handle_read(topic)
		except:
			logging.error('[Request] Error in request: {} {}'.format(msg.topic, msg.payload))
			traceback.print_exc()

	def _handle_write(self, topic, payload):
		logging.debug('[Write] Writing {} to {}'.format(payload, topic))
		value = json.loads(payload)['value']
		service, device_instance, path = self._get_uid_by_topic(topic)
		if service is None:
			raise Exception('Unknown service')

		self._set_dbus_value(service, '/' + path, value)

		# Run the queue as soon as possible
		GLib.idle_add(self._service_queue)

	def _handle_read(self, topic):
		logging.debug('[Read] Topic {}'.format(topic))
		service, device_instance, path = self._get_uid_by_topic(topic)
		if service is None:
			raise Exception('Unknown service')

		# Read a fresh value and make sure item is added. This is because a path
		# may not always send PropertiesChanged (eg vebus/Hub4/L1/AcPowerSetpoint)
		# but can nevertheless be read.
		value = self._get_dbus_value(service, '/' + path)
		if self._add_item(service, device_instance, path, value=value) == topic:
			self._publish(topic, value)

			# Run the queue as soon as possible
			GLib.idle_add(self._service_queue)

	def _get_uid_by_topic(self, topic):
		action, system_id, service_type, device_instance, path = topic.split('/', 4)
		device_instance = int(device_instance)
		service = self._services.get('{}/{}'.format(service_type, device_instance))
		return service, device_instance, path

	def _dbus_name_owner_changed(self, name, oldowner, newowner):
		if not name.startswith('com.victronenergy.'):
			return
		if newowner != '':
			self._scan_dbus_service(name, publish=True)
			self._service_ids[newowner] = name
		elif oldowner != '':
			logging.info('[OwnerChange] Service disappeared: {}'.format(name))
			for path, topic in list(self._topics.items()):
				if path.startswith(name + '/'):
					self._unpublish(topic)
					del self._topics[path]
					del self._values[topic]
			if name in self._services:
				del self._services[name]
			if oldowner in self._service_ids:
				del self._service_ids[oldowner]

	def _scan_dbus_service(self, service, publish=False):
		try:
			logging.info('[Scanning] service: {}'.format(service))
			try:
				device_instance = int(self._get_dbus_value(service, '/DeviceInstance'))
			except dbus.exceptions.DBusException as e:
				if e.get_dbus_name() == 'org.freedesktop.DBus.Error.UnknownObject' or \
					e.get_dbus_name() == 'org.freedesktop.DBus.Error.UnknownMethod':
					device_instance = 0
				else:
					raise
			except TypeError:
				device_instance = 0
			short_service_name = get_short_service_name(service, device_instance)
			self._services[short_service_name] = service
			try:
				items = self._get_dbus_value(service, '/')
			except dbus.exceptions.DBusException as e:
				if e.get_dbus_name() == 'org.freedesktop.DBus.Error.UnknownObject' or \
					e.get_dbus_name() == 'org.freedesktop.DBus.Error.UnknownMethod':
					self._introspect(service, device_instance, '/', publish)
					logging.warn('[Scanning] {} does not provide an item listing'.format(service))
					return
				else:
					raise

			if isinstance(items, dict):
				for path, value in items.items():
					v = unwrap_dbus_value(value)
					topic = self._add_item(service, device_instance, path, value=v)
					if publish and topic is not None:
						self._publish(topic, v)

		except dbus.exceptions.DBusException as e:
			if e.get_dbus_name() == 'org.freedesktop.DBus.Error.ServiceUnknown' or \
				e.get_dbus_name() == 'org.freedesktop.DBus.Error.Disconnected':
				logging.info("[Scanning] Service disappeared while being scanned: %s", service)
			elif e.get_dbus_name() == 'org.freedesktop.DBus.Error.NoReply':
				logging.info("[Scanning] No response from service during scan: %s", service)
			else:
				raise

	def _introspect(self, service, device_instance, path, publish=True):
		value = self._dbus_conn.call_blocking(service, path, None, 'Introspect', '', [])
		tree = etree.fromstring(value)
		nodes = tree.findall('node')
		if len(nodes) == 0:
			for iface in tree.findall('interface'):
				if iface.attrib.get('name') == 'com.victronenergy.BusItem':
					v = self._get_dbus_value(service, path)
					topic = self._add_item(service, device_instance, path, value=v)
					if publish and topic is not None:
						self._publish(topic, v)
		else:
			for child in nodes:
				name = child.attrib.get('name')
				if name is not None:
					if path.endswith('/'):
						p = path + name
					else:
						p = path + '/' + name
					self._introspect(service, device_instance, p, publish=publish)

	def _on_dbus_value_changed(self, changes, path=None, service_id=None):
		service = self._service_ids.get(service_id)
		if service is None:
			return
		uid = service + path
		topic = self._topics.get(uid)
		if topic is None:
			for service_short_name, service_name in self._services.items():
				if service_name == service:
					device_instance = service_short_name.split('/')[1]
					self._add_item(service, device_instance, path)
					logging.info('New item found: {}{}'.format(service_short_name, path))
					topic = self._topics[uid]
					break
			else:
				return
		value = changes.get("Value")
		if value is None:
			return
		value = unwrap_dbus_value(value)
		self._values[topic] = value
		self._publish(topic, value)

	def _timer_service_queue(self):
		if len(self.queue) > 0 and time() - self._last_queue_run > 1.5:
			if self._service_queue():
				# The queue is not empty
				GLib.idle_add(self._service_queue)
		return True

	def _service_queue(self, items=5):
		self._last_queue_run = time()
		for _ in six.moves.range(items):
			try:
				topic, value = self.queue.popitem(last=False)
			except KeyError:
				return False
			else:
				try:
					self._client.publish(topic, value, retain=True)
				except:
					logging.error('[Queue] Error publishing: {} {}'.format(topic, value))
					traceback.print_exc()

		return True

	def _add_item(self, service, device_instance, path, value=None):
		if not path.startswith('/'):
			path = '/' + path
		uid = service + path
		r = self._topics.get(uid)
		if r is not None:
			# Topic exist already
			return r

		service_type = get_service_type(service)
		if (service_type, path) in blocked_items:
			return None

		self._topics[uid] = topic = 'N/{}/{}/{}{}'.format(self._system_id, service_type, device_instance, path)
		self._values[topic] = value
		return topic

	def _get_dbus_value(self, service, path):
		value = self._dbus_conn.call_blocking(service, path, None, 'GetValue', '', [])
		return unwrap_dbus_value(value)

	def _set_dbus_value(self, service, path, value):
		value = wrap_dbus_value(value)
		return self._dbus_conn.call_blocking(service, path, None, 'SetValue', 'v', [value])

def get_service_type(service_name):
	if not service_name.startswith(ServicePrefix):
		raise Exception('No victron service')
	return service_name.split('.')[2]


def get_service_base_name(service_name):
	if not service_name.startswith(ServicePrefix):
		raise Exception('No victron service')
	return '.'.join(service_name.split('.')[0:3])


def get_short_service_name(service, device_instance):
	return '{}/{}'.format(get_service_type(service), device_instance)


def dumpstacks(signal, frame):
	import threading
	id2name = dict((t.ident, t.name) for t in threading.enumerate())
	for tid, stack in sys._current_frames().items():
		logging.info ("=== {} ===".format(id2name[tid]))
		traceback.print_stack(f=stack)

def exit(signal, frame):
	sys.exit(0)

def main():
	signal.signal(signal.SIGINT, exit)

	parser = argparse.ArgumentParser(description='Publishes values from the D-Bus to an MQTT broker')
	parser.add_argument('-d', '--debug', help='set logging level to debug', action='store_true')
	parser.add_argument('-q', '--mqtt-server', nargs='?', default=None, help='name of the mqtt server')
	parser.add_argument('-u', '--mqtt-user', default=None, help='mqtt user name')
	parser.add_argument('-P', '--mqtt-password', default=None, help='mqtt password')
	parser.add_argument('-c', '--mqtt-certificate', default=None, help='path to CA certificate used for SSL communication')
	parser.add_argument('-b', '--dbus', default=None, help='dbus address')
	parser.add_argument('-k', '--keep-alive', default=60, help='keep alive interval in seconds', type=int)
	parser.add_argument('-i', '--init-broker', action='store_true', help='Tries to setup communication with VRM MQTT broker')
	args = parser.parse_args()

	print("-------- dbus_mqtt, v{} is starting up --------".format(SoftwareVersion))
	logger = setup_logging(args.debug)

	mainloop = GLib.MainLoop()
	# Have a mainloop, so we can send/receive asynchronous calls to and from dbus
	DBusGMainLoop(set_as_default=True)
	handler = DbusMqtt(
		mqtt_server=args.mqtt_server, ca_cert=args.mqtt_certificate, user=args.mqtt_user,
		passwd=args.mqtt_password, dbus_address=args.dbus,
		init_broker=args.init_broker, debug=args.debug)

	# Handle SIGUSR1 and dump a stack trace
	signal.signal(signal.SIGUSR1, dumpstacks)

	# Start and run the mainloop
	try:
		mainloop.run()
	except KeyboardInterrupt:
		pass

if __name__ == '__main__':
	main()
