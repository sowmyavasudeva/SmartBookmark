# Copyright 2017 Mycroft AI Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import time
import datetime
from Queue import Queue, Empty
from threading import Thread

import speech_recognition as sr
from pyee import EventEmitter
from requests import HTTPError
from requests.exceptions import ConnectionError

import mycroft.dialog
from mycroft.client.speech.hotword_factory import HotWordFactory
from mycroft.client.speech.mic import MutableMicrophone, ResponsiveRecognizer
from mycroft.client.speech.recognizer.pocketsphinx_recognizer import PocketsphinxRecognizer
from mycroft.configuration import ConfigurationManager
from mycroft.metrics import MetricsAggregator
from mycroft.session import SessionManager
from mycroft.stt import STTFactory
from mycroft.util.log import LOG


class AudioProducer(Thread):
    """
    AudioProducer
    given a mic and a recognizer implementation, continuously listens to the
    mic for potential speech chunks and pushes them onto the queue.
    """

    def __init__(self, state, queue, mic, recognizer, emitter):
        super(AudioProducer, self).__init__()
        self.daemon = True
        self.state = state
        self.queue = queue
        self.mic = mic
        self.recognizer = recognizer
        self.emitter = emitter

    def run(self):
        with self.mic as source:
            self.recognizer.adjust_for_ambient_noise(source)
            while self.state.running:
                LOG.info("Microphone listening started")
                try:
                    audio = self.recognizer.listen(source, self.emitter)
                    self.queue.put(audio)
                except IOError, ex:
                    # NOTE: Audio stack on raspi is slightly different, throws
                    # IOError every other listen, almost like it can't handle
                    # buffering audio between listen loops.
                    # The internet was not helpful.
                    # http://stackoverflow.com/questions/10733903/pyaudio-input-overflowed
                    self.emitter.emit("recognizer_loop:ioerror", ex)

    def stop(self):
        """
            Stop producer thread.
        """
        self.state.running = False
        self.recognizer.stop()


class AudioConsumer(Thread):
    """
    AudioConsumer
    Consumes AudioData chunks off the queue
    """

    # In seconds, the minimum audio size to be sent to remote STT
    MIN_AUDIO_SIZE = 0.5

    def __init__(self, state, queue, emitter, stt,
                 wakeup_recognizer, wakeword_recognizer):
        super(AudioConsumer, self).__init__()
        self.daemon = True
        self.queue = queue
        self.state = state
        self.emitter = emitter
        self.stt = stt
        self.wakeup_recognizer = wakeup_recognizer
        self.wakeword_recognizer = wakeword_recognizer
        self.metrics = MetricsAggregator()
        self.config = ConfigurationManager.get()
        emitter.on("recognizer_loop:hotword", self.set_word)

    def set_word(self, event):
        if event.get("start_listening"):
                 # set new hot word
                 self.hotword = event.get("hotword", self.wakeword_recognizer.key_phrase)

    def run(self):
        while self.state.running:
            self.read()

    def read(self):
        try:
            audio = self.queue.get(timeout=0.5)
        except Empty:
            return

        if audio is None:
            return

        if self.state.sleeping:
            self.wake_up(audio)
        else:
            self.process(audio)

    # TODO: Localization
    def wake_up(self, audio):
        if self.wakeup_recognizer.found_wake_word(audio.frame_data):
            SessionManager.touch()
            self.state.sleeping = False
            self.__speak(mycroft.dialog.get("i am awake", self.stt.lang))
            self.metrics.increment("mycroft.wakeup")

    @staticmethod
    def _audio_length(audio):
        return float(len(audio.frame_data)) / (
            audio.sample_rate * audio.sample_width)

    # TODO: Localization
    def process(self, audio):
        SessionManager.touch()
	"""
	if self.hotword:
            word = self.hotword
            self.hotword = None
        else:
	"""
        word = self.wakeword_recognizer.key_phrase
        payload = {
            'utterance': word,
            'session': SessionManager.get().session_id,
        }
        self.emitter.emit("recognizer_loop:wakeword", payload)

        if self._audio_length(audio) < self.MIN_AUDIO_SIZE:
            LOG.warning("Audio too short to be processed")
        else:
            self.transcribe(audio)

    def transcribe(self, audio):
        LOG.debug("Transcribing audio")
        text = None
        try:
            # Invoke the STT engine on the audio clip
            text = self.stt.execute(audio).lower().strip()
            LOG.debug("STT: --------->" + text)
        except sr.RequestError as e:
            LOG.error("Could not request Speech Recognition {0}".format(e))
        except ConnectionError as e:
            LOG.error("Connection Error: {0}".format(e))
            self.emitter.emit("recognizer_loop:no_internet")
        except HTTPError as e:
            if e.response.status_code == 401:
                text = "pair my device"  # phrase to start the pairing process
                LOG.warning("Access Denied at mycroft.ai")
        except Exception as e:
            LOG.error(e)
            LOG.error("Speech Recognition could not understand audio")
        if text:
            # STT succeeded, send the transcribed speech on for processing
            LOG.error("maine samjha tune bola "+ text)
            tellMeMore = "tell me more"
            if(text == tellMeMore):
                #hotWordListener = self.finalHotWord
                LOG.info("found tell me more in listener****")
                #text = text + " about " + hotWordListener
                with open("hotWordFile.txt", "rw+") as hotWordTemp:
                        prevHotWord = hotWordTemp.read()
                        hotWordTemp.truncate(0)
                        text = "tell me about " + prevHotWord
                        LOG.error(" naya wala maine samjha tune bola "+ text)

            payload = {
                'utterances': [text],
                'lang': self.stt.lang,
                'session': SessionManager.get().session_id
            }
            self.emitter.emit("recognizer_loop:utterance", payload)
            self.metrics.attr('utterances', [text])

    def __speak(self, utterance):
        payload = {
            'utterance': utterance,
            'session': SessionManager.get().session_id
        }
        self.emitter.emit("speak", payload)


class RecognizerLoopState(object):
    def __init__(self):
        self.running = False
        self.sleeping = False


class RecognizerLoop(EventEmitter):
    """
        EventEmitter loop running speech recognition. Local wake word
        recognizer and remote general speech recognition.
    """

    def __init__(self):
        super(RecognizerLoop, self).__init__()
        self.mute_calls = 0
        self._load_config()

    def _load_config(self):
        """
            Load configuration parameters from configuration
        """
        LOG.info("*********loading configuration")
        config = ConfigurationManager.get()
        self.config_core = config
        self._config_hash = hash(str(config))
        self.lang = config.get('lang')
        self.config = config.get('listener')
        rate = self.config.get('sample_rate')
        device_index = self.config.get('device_index')

        self.microphone = MutableMicrophone(device_index, rate,
                                            mute=self.mute_calls > 0)
        # FIXME - channels are not been used
        self.microphone.CHANNELS = self.config.get('channels')
        self.wakeword_recognizer = self.create_wake_word_recognizer(rate,self.lang)
        # TODO - localization
        self.wakeup_recognizer = self.create_wakeup_recognizer(rate,self.lang)
        self.hot_word_engines = {}
        self.create_hot_word_engines()
        self.wakeup_recognizer = self.create_wakeup_recognizer(rate, self.lang)
        self.responsive_recognizer = ResponsiveRecognizer(
            self.wakeword_recognizer, self.hot_word_engines)
        self.state = RecognizerLoopState()

    def create_hot_word_engines(self):
        LOG.info("###############********creating hotword engines************************************************************************")
        hot_words = self.config_core.get("hotwords", {})
        for word in hot_words:
                     print("wake word creation for one word started:-"+str(datetime.datetime.now()))
                     data = hot_words[word]
                     print("#######################xx***********************************found wake word************************************************************************")
                     #if not data.get("active", True):
                         #continue
                     engine_type = data["module"]
                     ding = data.get("sound")
                     utterance = data.get("utterance")
                     listen = data.get("listen", True)
                     # data = data["data"]
                     LOG.info("Creating hotword engine for " + word)
                     lang = self.config_core.get("lang", "en-us")
		     print("@@@@@@@@@@@ lang @@@@@@@@@@@@@@@@@@@@@",lang)
                     # rate = self.config.get("rate")
                     hot_word = data.get("hot_word")
                     print("@@@@@@@@@@@ hot_word @@@@@@@@@@@@@@@@@@@@@",hot_word)
                     phonemes = data.get('phonemes')
		     print("@@@@@@@@@@@ phonemes @@@@@@@@@@@@@@@@@@@@@",phonemes)
                     threshold = data.get('threshold')
                     engine = PocketsphinxRecognizer(hot_word, phonemes,
                                                     threshold, 16000, lang)
                     print("wake word for one word finished:-"+str(datetime.datetime.now()))
		     if engine is None:
				print("engine is null!!!!!!")
                     self.hot_word_engines[word] = [engine, ding, utterance,
                                                   engine_type]


    def create_wake_word_recognizer(self, rate, lang):
        # Create a local recognizer to hear the wakeup word, e.g. 'Hey Mycroft'
        LOG.info("creating wake word engine")
        word = self.config.get("wake_word", "hey mycroft")
        # TODO remove this, only for server settings compatibility
        phonemes = self.config.get("phonemes")
        thresh = self.config.get("threshold")
        config = self.config_core.get("hotwords", {word: {}})
        if word not in config:
            config[word] = {}
        if phonemes:
            config[word]["phonemes"] = phonemes
        if thresh:
            config[word]["threshold"] = thresh
        if phonemes is None or thresh is None:
            config = None
        return HotWordFactory.create_hotword(word, config, self.lang)

    def create_wakeup_recognizer(self, rate, lang):
        LOG.info("creating stand up word engine")
        word = self.config.get("stand_up_word", "wake up")
        return HotWordFactory.create_hotword(word, lang=self.lang)

    def start_async(self):
        """
            Start consumer and producer threads
        """
        self.state.running = True
        queue = Queue()
        self.producer = AudioProducer(self.state, queue, self.microphone,
                                      self.responsive_recognizer, self)
        self.producer.start()
        self.consumer = AudioConsumer(self.state, queue, self,
                                      STTFactory.create(),
                                      self.wakeup_recognizer,
                                      self.wakeword_recognizer)
        self.consumer.start()

    def stop(self):
        self.state.running = False
        self.producer.stop()
        # wait for threads to shutdown
        self.producer.join()
        self.consumer.join()

    def mute(self):
        """
            Mute microphone and increase number of requests to mute
        """
        self.mute_calls += 1
        if self.microphone:
            self.microphone.mute()

    def unmute(self):
        """
            Unmute mic if as many unmute calls as mute calls have been
            received.
        """
        if self.mute_calls > 0:
            self.mute_calls -= 1

        if self.mute_calls <= 0 and self.microphone:
            self.microphone.unmute()
            self.mute_calls = 0

    def force_unmute(self):
        """
            Completely unmute mic dispite the number of calls to mute
        """
        self.mute_calls = 0
        self.unmute()

    def is_muted(self):
        if self.microphone:
            return self.microphone.is_muted()
        else:
            return True  # consider 'no mic' muted

    def sleep(self):
        self.state.sleeping = True

    def awaken(self):
        self.state.sleeping = False

    def run(self):
        self.start_async()
        while self.state.running:
            try:
                time.sleep(1)
                if self._config_hash != hash(
                        str(ConfigurationManager().get())):
                    LOG.debug('Config has changed, reloading...')
                    self.reload()
            except KeyboardInterrupt as e:
                LOG.error(e)
                self.stop()
                raise  # Re-raise KeyboardInterrupt

    def reload(self):
        """
            Reload configuration and restart consumer and producer
        """
        self.stop()
        # load config
        self._load_config()
        # restart
        self.start_async()
