# ###################################################
# Copyright (C) 2012 The Unknown Horizons Team
# team@unknown-horizons.org
# This file is part of Unknown Horizons.
#
# Unknown Horizons is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the
# Free Software Foundation, Inc.,
# 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA
# ###################################################

import logging
import uuid
from collections import defaultdict

import horizons.globals

from horizons import network
from horizons.constants import NETWORK, VERSION, LANGUAGENAMES
from horizons.extscheduler import ExtScheduler
from horizons.messaging.messagebus import SimpleMessageBus
from horizons.network import CommandError, NetworkException, FatalError, packets
from horizons.network.common import Game
from horizons.network.connection import Connection
from horizons.util.color import Color
from horizons.util.difficultysettings import DifficultySettings
from horizons.util.python import parse_port
from horizons.util.python.singleton import ManualConstructionSingleton


class ClientMode(object):
	Server = 0
	Game = 1


class ClientData(object):
	def __init__(self):
		self.name = horizons.globals.fife.get_uh_setting("Nickname")
		self.color = horizons.globals.fife.get_uh_setting("ColorID")
		self.version = VERSION.RELEASE_VERSION

		try:
			self.id = uuid.UUID(horizons.globals.fife.get_uh_setting("ClientID")).hex
		except (ValueError, TypeError):
			# We need a new client id
			client_id = uuid.uuid4()
			horizons.globals.fife.set_uh_setting("ClientID", client_id)
			horizons.globals.fife.save_settings()
			self.id = client_id.hex


class NetworkInterface(object):
	"""Interface for low level networking"""
	__metaclass__ = ManualConstructionSingleton

	log = logging.getLogger("network")

	PING_INTERVAL = 0.5 # ping interval in seconds

	def __init__(self):
		self._mode = None
		self.sid = None
		self.capabilities = None
		self._game = None

		self.__setup_client()

		message_types = ('lobbygame_chat', 'lobbygame_join', 'lobbygame_leave',
		                 'lobbygame_terminate', 'lobbygame_toggleready',
		                 'lobbygame_changename', 'lobbygame_kick',
		                 'lobbygame_changecolor', 'lobbygame_state',
		                 'lobbygame_starts', 'game_starts', 
		                 'game_details_changed', 'game_prepare', 'error')

		self._messagebus = SimpleMessageBus(message_types)
		self.subscribe = self._messagebus.subscribe
		self.broadcast = self._messagebus.broadcast

		# create a game_details_changed callback
		for t in ('lobbygame_join', 'lobbygame_leave', 'lobbygame_changename',
		          'lobbygame_changecolor', 'lobbygame_toggleready'):
			self.subscribe(t, lambda *a, **b: self.broadcast("game_details_changed"))

		self.subscribe("lobbygame_starts", self._on_lobbygame_starts)
		self.subscribe('lobbygame_changename',  self._on_change_name)
		self.subscribe('lobbygame_changecolor', self._on_change_color)

		self.received_packets = []

		ExtScheduler().add_new_object(self.ping, self, self.PING_INTERVAL, -1)

		self._client_data = ClientData()

	def get_game(self):
		game = self._game
		if game is None:
			return None
		return self.game2mpgame(game)

	def isconnected(self):
		return self._connection.is_connected

	def isjoined(self):
		return self._game is not None

	def network_data_changed(self, connect=False):
		"""Call in case constants like client address or client port changed.
		@param connect: whether to connect after the data updated
		@throws RuntimeError in case of invalid data or an NetworkException forwarded from connect"""

		if self.isconnected():
			self.disconnect()
		self.__setup_client()
		if connect:
			self.connect()

	def __setup_client(self):
		serveraddress = [NETWORK.SERVER_ADDRESS, NETWORK.SERVER_PORT]
		clientaddress = None
		client_port = parse_port(horizons.globals.fife.get_uh_setting("NetworkPort"))
		if NETWORK.CLIENT_ADDRESS is not None or client_port > 0:
			clientaddress = [NETWORK.CLIENT_ADDRESS, client_port]
		try:
			self._connection = Connection(self.process_async_packet, serveraddress, clientaddress)
		except NetworkException as e:
			raise RuntimeError(e)

	def get_client_name(self):
		return self._client_data.name

	def get_client_color(self):
		return self._client_data.color

	def get_clientversion(self):
		return self._client_data.version

	def connect(self):
		"""
		@throws: NetworkError
		"""
		try:
			packet = self._connection.connect()
			self.sid = packet[1].sid
			self.capabilities = packet[1].capabilities
			self._mode = ClientMode.Server
			self.log.debug("[CONNECT] done (session=%s)" % (self.sid))
			self.set_client_language()
		except NetworkException as e:
			self.disconnect()
			raise e

	def disconnect(self):
		self._mode = None
		self._connection.disconnect()

	def ping(self):
		"""calls _client.ping until all packets are received"""
		if self._connection.is_connected:
			try:
				while self._connection.ping(): # ping receives packets
					pass
			except NetworkException as e:
				self._handle_exception(e)

	def set_props(self, props):
		try:
			self.log.debug("[SETPROPS]")
			self._assert_connection()
			self._connection.send(packets.client.cmd_sessionprops(props))
			self._connection.receive_packet(packets.cmd_ok)
		except NetworkException as e:
			self._handle_exception(e)
			return False
		return True

	def set_client_language(self):
		lang = LANGUAGENAMES.get_by_value(horizons.globals.fife.get_uh_setting("Language"))
		if len(lang):
			return self.set_props({'lang': lang})
		return True

	def creategame(self, mapname, maxplayers, gamename, maphash="", password=""):
		self.log.debug("[CREATEGAME] %s(h=%s), %s, %s, %s", mapname, maphash, maxplayers, gamename)
		try:
			self._assert_connection()
			self.log.debug("[CREATE] mapname=%s maxplayers=%d" % (mapname, maxplayers))
			self._connection.send(packets.client.cmd_creategame(
				clientver=self._client_data.version,
				clientid=self._client_data.id,
				playername=self._client_data.name,
				playercolor=self._client_data.color,
				gamename=gamename,
				mapname=mapname,
				maxplayers=maxplayers,
				maphash=maphash,
				password=password
			))
			packet = self._connection.receive_packet(packets.server.data_gamestate)
			game = self._game = packet[1].game
		except NetworkException as e:
			self._handle_exception(e)
			return None
		return self.game2mpgame(game)

	def joingame(self, uuid, password="", fetch=False):
		"""Join a game with a certain uuid"""
		i = 2
		try:
			while i < 10: # FIXME: try 10 different names and colors
				try:
					self._joingame(uuid, password, fetch)
					return True
				except CommandError as e:
					self.log.debug("NetworkInterface: failed to join")
					if 'name' in e.message:
						self.change_name(self._client_data.name + unicode(i), save=False )
					elif 'color' in e.message:
						self.change_color(self._client_data.color + i, save=False)
					else:
						raise
				i += 1
			self._joingame(uuid, password, fetch)
		except NetworkException as e:
			self._handle_exception(e)
		return False

	def _joingame(self, uuid, password="", fetch=False):
		self._assert_connection()
		self.log.debug("[JOIN] %s" % (uuid))
		self._connection.send(packets.client.cmd_joingame(
			uuid=uuid,
			clientver=self._client_data.version,
			clientid=self._client_data.id,
			playername=self._client_data.name,
			playercolor=self._client_data.color,
			password=password,
			fetch=fetch
		))
		packet = self._connection.receive_packet(packets.server.data_gamestate)
		self._game = packet[1].game
		return self._game

	def leavegame(self):
		try:
			self._assert_connection()
			self._assert_lobby()
			self.log.debug("[LEAVE]")
			self._connection.send(packets.client.cmd_leavegame())
			self._connection.receive_packet(packets.cmd_ok)
			self._game = None
		except NetworkException as e:
			fatal = self._handle_exception(e)
			if fatal:
				return False
		return True

	def chat(self, message):
		try:
			self._assert_connection()
			self._assert_lobby()
			self.log.debug("[CHAT] %s" % (message))
			self._connection.send(packets.client.cmd_chatmsg(message))
		except NetworkException as e:
			self._handle_exception(e)
			return False
		return True

	def change_name(self, new_name, save=True):
		if save:
			horizons.globals.fife.set_uh_setting("Nickname", new_name)
			horizons.globals.fife.save_settings()

		try:
			if self._client_data.name == new_name:
				return True
			self.log.debug("[CHANGENAME] %s" % (new_name))
			if self._mode is None or self._game is None:
				self._client_data.name = new_name
				return True
			self._connection.send(packets.client.cmd_changename(new_name))
			return False
		except NetworkException as e:
			self._handle_exception(e)
			return False

	def change_color(self, new_color, save=True):
		if new_color > len(set(Color)):
			new_color %= len(set(Color))
		if save:
			horizons.globals.fife.set_uh_setting("ColorID", new_color)
			horizons.globals.fife.save_settings()

		try:
			if self._client_data.color == new_color:
				return True
			self.log.debug("[CHANGECOLOR] %s" % (new_color))
			if self._mode is None or self._game is None:
				self._client_data.color = new_color
				return True
			self._connection.send(packets.client.cmd_changecolor(new_color))
			return False
		except NetworkException as e:
			self._handle_exception(e)
			return False

	def _on_lobbygame_starts(self, game):
		game = self.get_game()
		self.broadcast("game_prepare", game)

	def _on_game_data(self, data):
		self.received_packets.append(data)

	def get_active_games(self, only_this_version_allowed=False):
		"""Returns a list of active games or None on fatal error"""
		ret_mp_games = []
		try:
			self._assert_connection()
			self.log.debug("[LIST]")
			version = self._client_data.version if only_this_version_allowed else -1
			self._connection.send(packets.client.cmd_listgames(version))
			packet = self._connection.receive_packet(packets.server.data_gameslist)
			games = packet[1].games
		except NetworkException as e:
			fatal = self._handle_exception(e)
			return [] if not fatal else None
		for game in games:
			ret_mp_games.append(self.game2mpgame(game))
			self.log.debug("NetworkInterface: found active game %s", game.mapname)
		return ret_mp_games

	def send_to_all_clients(self, packet):
		"""
		Sends packet to all players, that are part of the game
		"""
		if self._connection.is_connected:
			try:
				self._send(packet)
			except NetworkException as e:
				self._handle_exception(e)

	def receive_all(self):
		"""
		Returns list of all packets, that have arrived until now (since the last call)
		@return: list of packets
		"""
		try:
			while self._connection.ping(): # ping receives packets
				pass
		except NetworkException as e:
			self.log.debug("ping in receive_all failed: "+str(e))
			self._handle_exception(e)
			raise CommandError(e)
		ret_list = self.received_packets
		self.received_packets = []
		return ret_list

	def game2mpgame(self, game):
		return MPGame(game, self)

	def _handle_exception(self, e):
		try:
			raise e
		except FatalError as e:
			self.broadcast("error", e, fatal=True)
			self.disconnect()
			return True
		except NetworkException as e:
			self.broadcast("error", e, fatal=False)
			return False

	def toggle_ready(self):
		self.log.debug("[TOGGLEREADY]")
		self._connection.send(packets.client.cmd_toggleready())

	def kick(self, player_sid):
		self.log.debug("[KICK]")
		self._connection.send(packets.client.cmd_kickplayer(player_sid))

	def _on_change_name(self, game, plold, plnew, myself):
		self.log.debug("[ONCHANGENAME] %s -> %s" % (plold.name, plnew.name))
		if myself:
			self._client_data.name = plnew.name

	def _on_change_color(self, game, plold, plnew, myself):
		self.log.debug("[ONCHANGECOLOR] %s: %s -> %s" % (plnew.name, plold.color, plnew.color))
		if myself:
			self._client_data.color = plnew.color

	def process_async_packet(self, packet):
		"""
		return True if packet was processed successfully
		return False if packet should be queue
		"""
		if packet is None:
			return True
		if isinstance(packet[1], packets.server.cmd_chatmsg):
			# ignore packet if we are not a game lobby
			if self._game is None:
				return True
			self.broadcast("lobbygame_chat", self._game, packet[1].playername, packet[1].chatmsg)
		elif isinstance(packet[1], packets.server.data_gamestate):
			# ignore packet if we are not a game lobby
			if self._game is None:
				return True
			self.broadcast("lobbygame_state", self._game, packet[1].game)

			oldplayers = list(self._game.players)
			self._game = packet[1].game

			# calculate changeset
			for pnew in self._game.players:
				found = None
				for pold in oldplayers:
					if pnew.sid == pold.sid:
						found = pold
						myself = (pnew.sid == self.sid)
						if pnew.name != pold.name:
							self.broadcast("lobbygame_changename", self._game, pold, pnew, myself)
						if pnew.color != pold.color:
							self.broadcast("lobbygame_changecolor", self._game, pold, pnew, myself)
						if pnew.ready != pold.ready:
							self.broadcast("lobbygame_toggleready", self._game, pold, pnew, myself)
						break
				if found is None:
					self.broadcast("lobbygame_join", self._game, pnew)
				else:
					oldplayers.remove(found)
			for pold in oldplayers:
				self.broadcast("lobbygame_leave", self._game, pold)
			return True
		elif isinstance(packet[1], packets.server.cmd_preparegame):
			# ignore packet if we are not a game lobby
			if self._game is None:
				return True
			self._on_game_prepare()
		elif isinstance(packet[1], packets.server.cmd_startgame):
			# ignore packet if we are not a game lobby
			if self._game is None:
				return True
			self._on_game_start()
		elif isinstance(packet[1], packets.client.game_data):
			self.log.debug("[GAMEDATA] from %s" % (packet[0].address))
			self._on_game_data(packet[1].data)
		elif isinstance(packet[1], packets.server.cmd_kickplayer):
			player = packet[1].player
			game = self._game
			myself = (player.sid == self.sid)
			if myself:
				# this will destroy self._game
				self._assert_connection()
				self._assert_lobby()
				self.log.debug("[LEAVE]")
				self._game = None
			self.broadcast("lobbygame_kick", game, player, myself)

		return False

	def _send(self, packet, channelid=0):
		if self._mode is ClientMode.Game:
			packet = packets.client.game_data(packet)

		self._connection.send(packet, channelid)

	def _on_game_prepare(self):
		self.log.debug("[GAMEPREPARE]")
		self._game.state = Game.State.Prepare
		self.broadcast("lobbygame_starts", self._game)
		self._send(packets.client.cmd_preparedgame())

	def _on_game_start(self):
		self.log.debug("[GAMESTART]")
		self._game.state = Game.State.Running
		self._mode = ClientMode.Game
		self.broadcast("game_starts", self._game)

	def _assert_connection(self):
		if self._mode is None:
			raise network.NotConnected()
		if self._mode is not ClientMode.Server:
			raise network.NotInServerMode("We are not in server mode")

	def _assert_lobby(self):
		if self._game is None:
			raise network.NotInGameLobby("We are not in a game lobby")


class MPGame(object):
	def __init__(self, game, netif):
		self.uuid       = game.uuid
		self.creator    = game.creator
		self.mapname    = game.mapname
		self.maphash    = game.maphash
		self.maxplayers = game.maxplayers
		self.playercnt  = game.playercnt
		self.players    = game.players
		self.version    = game.clientversion
		self.name       = game.name
		self.password   = game.password
		self.netif      = netif

	def get_uuid(self):
		return self.uuid

	def get_map_name(self):
		return self.mapname

	def get_map_hash(self):
		return self.maphash

	def is_savegame(self):
		return bool(self.maphash)

	def get_name(self):
		return self.name

	def get_creator(self):
		return self.creator

	def get_player_limit(self):
		return self.maxplayers

	def get_players(self):
		return self.players

	def has_password(self):
		return self.password

	def get_player_list(self):
		ret_players = []
		id = 1
		for player in self.get_players():
			# TODO: add support for selecting difficulty levels to the GUI
			status = _('Ready') if player.ready else _('Not Ready')
			ret_players.append({
				'id':         id,
				'sid':        player.sid,
				'name':       player.name,
				'color':      Color[player.color],
				'clientid':   player.clientid,
				'local':      self.netif.get_client_name() == player.name,
				'ai':         False,
				'difficulty': DifficultySettings.DEFAULT_LEVEL,
				'status':     status
				})
			id += 1
		return ret_players

	def get_player_count(self):
		return self.playercnt

	def get_version(self):
		return self.version

	def __str__(self):
		return "%s (%d/%d)" % (self.get_map_name(), self.get_player_count(), self.get_player_limit())
