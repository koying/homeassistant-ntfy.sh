# -*- coding: utf-8 -*-

#################################################################################################

import logging
import websocket
from threading import Thread
import json

##################################################################################################

_LOGGER = logging.getLogger(__name__)

##################################################################################################


class NtfyWsClient(object):
    def __init__(self, host, topic):
        # websocket.enableTrace(True)
        self.ws = websocket.WebSocketApp(
                    "wss://" + host + "/" + topic + "/ws",
                    on_message = lambda ws,msg: self.on_message(ws, msg),
                    on_error   = lambda ws,msg: self.on_error(ws, msg),
                    on_close   = lambda ws:     self.on_close(ws),
                    on_open    = lambda ws:     self.on_open(ws)
                )
        self.key = host + topic
        self.callback = None

        Thread(target=self.ws.run_forever).start()

    def on_message(self, ws, msg):
        try:
            payload = json.loads(msg)
            _LOGGER.debug("Event: %s", payload)
            if self.callback:
                self.callback(self.key, payload)
        except json.decoder.JSONDecodeError as e:
            # data may be encrypted
            _LOGGER.debug(msg.hex())
            _LOGGER.error(e)
        

    def on_error(self, ws, error):
        _LOGGER.error(error)

    def on_close(self, ws):
        _LOGGER.info("Connection Closed")

    def on_open(self, ws):
        _LOGGER.info("Connection Opened")
