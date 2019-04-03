from .pdmutils import *
from .nonce import *
from .radio import Radio
from .message import Message, MessageType
from .exceptions import PdmError, OmnipyError
from .definitions import *
from .packet_radio import TxPower

from decimal import *
import time
import struct
from datetime import datetime, timedelta


class Pdm:
    def __init__(self, pod):
        self.pod = pod
        self.nonce = None
        self.radio = None
        self.logger = getLogger()

    def get_nonce(self):
        if self.nonce is None and self.pod is not None and \
                 self.pod.id_lot is not None and self.pod.id_t is not None:
            self.nonce = Nonce(self.pod.id_lot, self.pod.id_t, self.pod.nonce_last, self.pod.nonce_seed)

        return self.nonce

    def get_radio(self):
        if self.radio is None:
            ps = 0
            ms = 0
            if self.pod is not None and self.pod.radio_message_sequence is not None:
                ms = self.pod.radio_message_sequence
            else:
                self.pod.radio_message_sequence = 0

            if self.pod is not None and self.pod.radio_packet_sequence is not None:
                ps = self.pod.radio_packet_sequence
            else:
                self.pod.radio_packet_sequence = 0

            self.radio = Radio(msg_sequence=ms, pkt_sequence=ps)

        return self.radio

    @staticmethod
    def customMessage(message_parts, with_nonce=False, lot=None, tid=None,
                      addr=0xFFFFFFFF, addr2=0xFFFFFFFF, nonce_seek=None, nonce_seed=None,
                      radio_message_sequence=0, radio_packet_sequence=0, tx_power=None, unknown_bits=0, radio=None):
        if radio is None:
            radio = Radio(radio_message_sequence, radio_packet_sequence)
        message = Message(MessageType.PDM, addr, addr2, sequence=radio_message_sequence)
        for command, body in message_parts:
            message.addCommand(command, body)

        message.unknownBits = unknown_bits
        nonce_obj = None
        if with_nonce:
            nonce_obj = Nonce(lot, tid, seekNonce=nonce_seek, seed=nonce_seed)
            nonce = nonce_obj.getNext()
            message.setNonce(nonce)
        try:
            response_message = radio.send_request_get_response(message, tx_power=tx_power)

            contents = response_message.getContents()
            for (ctype, content) in contents:
                if ctype == 0x06 and content[0] == 0x14:
                    getLogger().debug("Bad nonce error - renegotiating")
                    nonce_sync_word = struct.unpack(">H", content[1:])[0]
                    nonce_obj.sync(nonce_sync_word, message.sequence)
                    radio.messageSequence = message.sequence
                    return Pdm.customMessage(message_parts, with_nonce=with_nonce, lot=lot, tid=tid,
                                addr=addr, addr2=addr2, nonce_seek=nonce_seek, nonce_seed=nonce_seed,
                                radio_message_sequence=message.sequence, radio_packet_sequence=radio_packet_sequence,
                                             tx_power=tx_power)

            return response_message
        except:
            getLogger().exception("Error while custom message")
            raise

    def updatePodStatus(self, update_type=0):
        try:
            self._assert_pod_address_assigned()
            with PdmLock():
                self.logger.debug("updating pod status")
                self._update_status(update_type)

        except OmnipyError:
            raise
        except Exception as e:
            raise PdmError("Unexpected error") from e
        finally:
            self._savePod()

    def acknowledge_alerts(self, alert_mask):
        try:
            self._assert_can_acknowledge_alerts()

            with PdmLock():
                self.logger.debug("acknowledging alerts with bitmask %d" % alert_mask)
                self._acknowledge_alerts(alert_mask)

        except OmnipyError:
            raise
        except Exception as e:
            raise PdmError("Unexpected error") from e
        finally:
            self._savePod()

    def is_busy(self):
        try:
            with PdmLock(0):
                return self._is_bolus_running()
        except PdmBusyError:
            return True
        except OmnipyError:
            raise
        except Exception as e:
            raise PdmError("Unexpected error") from e

    def bolus(self, bolus_amount):
        try:
            with PdmLock():
                self._update_status()
                self._assert_pod_address_assigned()
                self._assert_can_generate_nonce()
                self._assert_immediate_bolus_not_active()
                self._assert_not_faulted()
                self._assert_status_running()

                if self.pod.var_maximum_bolus is not None and bolus_amount > self.pod.var_maximum_bolus:
                    raise PdmError("Bolus exceeds defined maximum bolus of %.2fU" % self.pod.var_maximum_bolus)

                pulseCount = int(bolus_amount * Decimal(20))

                if pulseCount == 0:
                    raise PdmError("Cannot do a zero bolus")

                pulseSpan = pulseCount * 16
                if pulseSpan > 0x3840:
                    raise PdmError("Bolus would exceed the maximum time allowed for an immediate bolus")

                if self._is_bolus_running():
                    raise PdmError("A previous bolus is already running")

                if bolus_amount > self.pod.insulin_reservoir:
                    raise PdmError("Cannot bolus %.2f units, insulin_reservoir capacity is at: %.2f")

                self._immediate_bolus(pulseCount, request_msg="BOLUS %02.2f" % float(bolus_amount))

                if self.pod.state_bolus != BolusState.Immediate:
                    raise PdmError("Pod did not confirm bolus")

                self.pod.last_enacted_bolus_start = time.time()
                self.pod.last_enacted_bolus_amount = float(bolus_amount)

        except OmnipyError:
            raise
        except Exception as e:
            raise PdmError("Unexpected error") from e
        finally:
            self._savePod()


    def cancelBolus(self, beep=False):
        try:
            with PdmLock():
                self._assert_pod_address_assigned()
                self._assert_can_generate_nonce()
                self._assert_not_faulted()
                self._assert_status_running()

                if self._is_bolus_running():
                    self.logger.debug("Canceling running bolus")
                    self._cancelActivity(cancelBolus=True, beep=beep)
                    if self.pod.state_bolus == BolusState.Immediate:
                        raise PdmError("Failed to cancel bolus")
                    else:
                        self.pod.last_enacted_bolus_amount = float(-1)
                        self.pod.last_enacted_bolus_start = time.time()
                else:
                    raise PdmError("Bolus is not running")

        except OmnipyError:
            raise
        except Exception as e:
            raise PdmError("Unexpected error") from e
        finally:
            self._savePod()

    def cancelTempBasal(self, beep=False):
        try:
            with PdmLock():
                self._update_status()
                self._assert_pod_address_assigned()
                self._assert_can_generate_nonce()
                self._assert_immediate_bolus_not_active()
                self._assert_not_faulted()
                self._assert_status_running()

                if self._is_temp_basal_active():
                    self.logger.debug("Canceling temp basal")
                    self._cancelActivity(cancelTempBasal=True, beep=beep)
                    if self.pod.state_basal == BasalState.TempBasal:
                        raise PdmError("Failed to cancel temp basal")
                    else:
                        self.pod.last_enacted_temp_basal_duration = float(-1)
                        self.pod.last_enacted_temp_basal_start = time.time()
                        self.pod.last_enacted_temp_basal_amount = float(-1)
                else:
                    self.logger.warning("Cancel temp basal received, while temp basal was not active. Ignoring.")

        except OmnipyError:
            raise
        except Exception as e:
            raise PdmError("Unexpected error") from e
        finally:
            self._savePod()

    def setTempBasal(self, basalRate, hours, confidenceReminder=False):
        try:
            with PdmLock():
                self._update_status()
                self._assert_pod_address_assigned()
                self._assert_can_generate_nonce()
                self._assert_immediate_bolus_not_active()
                self._assert_not_faulted()
                self._assert_status_running()

                halfHours = int(hours * Decimal(2))

                if halfHours > 24 or halfHours < 1:
                    raise PdmError("Requested duration is not valid")

                if self.pod.var_maximum_temp_basal_rate is not None and \
                        basalRate > Decimal(self.pod.var_maximum_temp_basal_rate):
                    raise PdmError("Requested rate exceeds maximum temp basal setting")
                if basalRate > Decimal(30):
                    raise PdmError("Requested rate exceeds maximum temp basal capability")

                if self._is_temp_basal_active():
                    self._cancelActivity(cancelTempBasal=True, beep=confidenceReminder)

                halfHourUnits = [basalRate / Decimal(2)] * halfHours
                pulseList = getPulsesForHalfHours(halfHourUnits)
                iseList = getInsulinScheduleTableFromPulses(pulseList)

                iseBody = getStringBodyFromTable(iseList)
                pulseBody = getStringBodyFromTable(pulseList)

                commandBody = struct.pack(">I", 0)
                commandBody += b"\x01"

                bodyForChecksum = bytes([halfHours])
                bodyForChecksum += struct.pack(">H", 0x3840)
                bodyForChecksum += struct.pack(">H", pulseList[0])
                checksum = getChecksum(bodyForChecksum + pulseBody)

                commandBody += struct.pack(">H", checksum)
                commandBody += bodyForChecksum
                commandBody += iseBody

                msg = self._createMessage(0x1a, commandBody)

                reminders = 0
                if confidenceReminder:
                    reminders |= 0x40

                commandBody = bytes([reminders])
                commandBody += b"\x00"

                pulseEntries = getPulseIntervalEntries(halfHourUnits)

                firstPulseCount, firstInterval, _ = pulseEntries[0]
                commandBody += struct.pack(">H", firstPulseCount)
                commandBody += struct.pack(">I", firstInterval)

                for pulseCount, interval, _ in pulseEntries:
                    commandBody += struct.pack(">H", pulseCount)
                    commandBody += struct.pack(">I", interval)

                msg.addCommand(0x16, commandBody)

                self._sendMessage(msg, with_nonce=True, request_msg="TEMPBASAL %02.2fU/h %02.1fh" % (float(basalRate),
                                                                                                 float(hours)))

                if self.pod.state_basal != BasalState.TempBasal:
                    raise PdmError("Failed to set temp basal")
                else:
                    self.pod.last_enacted_temp_basal_duration = float(hours)
                    self.pod.last_enacted_temp_basal_start = time.time()
                    self.pod.last_enacted_temp_basal_amount = float(basalRate)

        except OmnipyError:
            raise
        except Exception as e:
            raise PdmError("Unexpected error") from e
        finally:
            self._savePod()

    def set_basal_schedule(self, schedule, hours=None, minutes=None, seconds=None):
        try:
            with PdmLock():
                # self._update_status()
                # self._assert_pod_address_assigned()
                # self._assert_can_generate_nonce()
                # self._assert_immediate_bolus_not_active()
                # self._assert_not_faulted()
                # self._assert_status_running()

                # if self._is_temp_basal_active():
                #     raise PdmError("Cannot change basal schedule while a temp. basal is active")

                self._assert_basal_schedule_is_valid(schedule)

                self._set_basal_schedule(schedule, hour=hours, minute=minutes, second=seconds)

                if self.pod.state_basal != BasalState.Program:
                    raise PdmError("Failed to set basal schedule")
                else:
                    self.pod.var_basal_schedule = schedule

        except OmnipyError:
            raise
        except Exception as e:
            raise PdmError("Unexpected error") from e
        finally:
            self._savePod()

    def set_basal_schedule_w_cancel(self, schedule, hours=None, minutes=None, seconds=None):
        try:
            with PdmLock():
                self._assert_basal_schedule_is_valid(schedule)

                self._cancelActivity(cancelBasal=True)

                self._set_basal_schedule(schedule, hour=hours, minute=minutes, second=seconds)

                if self.pod.state_basal != BasalState.Program:
                    raise PdmError("Failed to set basal schedule")
                else:
                    self.pod.var_basal_schedule = schedule

        except OmnipyError:
            raise
        except Exception as e:
            raise PdmError("Unexpected error") from e
        finally:
            self._savePod()

    def deactivate_pod(self):
        try:
            with PdmLock():
                msg = self._createMessage(0x1c, bytes([0, 0, 0, 0]))
                self._sendMessage(msg, with_nonce=True, request_msg="DEACTIVATE POD")

        except OmnipyError:
            raise
        except Exception as e:
            raise PdmError("Unexpected error") from e
        finally:
            self._savePod()

    def activate_pod(self, candidate_address):
        try:
            return
            with PdmLock():
                self._assert_pod_activate_can_start()

                radio = self.get_radio()
                if radio is None:
                    raise PdmError("Cannot create radio instance")

                candidate_bytes = struct.pack(">I", candidate_address)

                radio.packetSequence = 0
                radio.messageSequence = 0
                self.pod.radio_address = 0xffffffff

                msg = self._createMessage(0x07, candidate_bytes, )
                self._sendMessage(msg, with_nonce=False, request_msg="ASSIGN ADDRESS 0x%08X" % candidate_address,
                                  tx_power=TxPower.Low)

                self._assert_pod_can_activate()

                command_body = candidate_bytes
                packet_timeout = 4
                command_body += bytes([0x14, packet_timeout])

                utc_offset = timedelta(minutes=self.pod.var_utc_offset)
                pod_date = datetime.utcnow() + utc_offset

                year = pod_date.year
                month = pod_date.month
                day = pod_date.day
                hour = pod_date.hour
                minute = pod_date.minute

                command_body += bytes([month, day, year - 2000, hour, minute])

                command_body += struct.pack(">I", self.pod.id_lot)
                command_body += struct.pack(">I", self.pod.id_t)

                msg = self._createMessage(0x03, command_body)
                self._sendMessage(msg, with_nonce=False, request_msg="PAIR POD", tx_power=TxPower.Low)

                self._assert_pod_paired()

                self.pod.nonce_seed = 0
                self.pod.nonce_last = None

                self.pod.radio_address = candidate_address

                if self.pod.var_alert_low_reservoir is not None:
                    self._configure_alert(PodAlertBit.LowReservoir,
                                          activate=True,
                                          trigger_auto_off=False,
                                          duration_minutes=0,
                                          trigger_reservoir=True,
                                          alert_after_reservoir=float(self.pod.var_alert_low_reservoir),
                                          beep_repeat_type=BeepPattern.OnceEveryMinuteForThreeMinutesAndRepeatHourly,
                                          beep_type=BeepType.BipBeepFourTimes)

                self._configure_alert(PodAlertBit.TimerLimit,
                                      activate=True,
                                      trigger_auto_off=False,
                                      duration_minutes=55,
                                      alert_after_minutes=5,
                                      beep_repeat_type=BeepPattern.OnceEveryMinuteForThreeMinutesAndRepeatEveryFifteenMinutes,
                                      beep_type=BeepType.BipBipBipTwice)

                self._set_delivery_flags(0, 0)

                self._immediate_bolus(52, pulse_speed=8, delivery_delay=1,
                                      request_msg="PRIMING 2.6U")

                time.sleep(55)

                if self.pod.var_alert_replace_pod is not None:
                    self._configure_alert(PodAlertBit.LowReservoir,
                                          activate=True,
                                          trigger_auto_off=False,
                                          duration_minutes=0,
                                          alert_after_minutes=int(self.pod.var_alert_replace_pod - self.pod.state_active_minutes),
                                          beep_repeat_type=BeepPattern.OnceEveryMinuteForThreeMinutesAndRepeatEveryFifteenMinutes,
                                          beep_type=BeepType.BipBeepFourTimes)
                else:
                    self._update_status()

                while self.pod.state_progress == PodProgress.Purging:
                    time.sleep(5)
                    self._update_status()

                if self.pod.state_progress != PodProgress.ReadyForInjection:
                    raise PdmError("Pod did not reach ready for injection stage")

        except OmnipyError:
            raise
        except Exception as e:
            raise PdmError("Unexpected error") from e
        finally:
            self._savePod()

    def inject_and_start(self, basal_schedule, hour, minute, second):
        try:
            return
            with PdmLock():
                if self.pod.state_progress != PodProgress.ReadyForInjection:
                    raise PdmError("Pod is not at the injection stage")

                self._assert_basal_schedule_is_valid(basal_schedule)

                self._set_basal_schedule(basal_schedule, hour=hour, minute=minute, second=second)

                if self.pod.state_progress != PodProgress.BasalScheduleSet:
                    raise PdmError("Pod did not acknowledge basal schedule")

                self._set_delivery_flags(0, 0)

                self._immediate_bolus(10, pulse_speed=8, delivery_delay=1,
                                      request_msg="INSERT CANNULA")

                if self.pod.state_progress != PodProgress.Inserting:
                    raise PdmError("Pod did not acknowledge cannula insertion start")

                time.sleep(10)

                while self.pod.state_progress == PodProgress.Inserting:
                    time.sleep(5)
                    self._update_status()

                if self.pod.state_progress != PodProgress.Running:
                    raise PdmError("Pod did not get to running state")

        except OmnipyError:
            raise
        except Exception as e:
            raise PdmError("Unexpected error") from e
        finally:
            self._savePod()

    def _set_delivery_flags(self, table5byte16, table5byte17):
        commandBody = bytes([0, 0, 0, 0, table5byte16, table5byte17])
        msg = self._createMessage(0x08, commandBody)
        self._sendMessage(msg, with_nonce=True, request_msg="SET DELIVERY FLAGS %d %d" %
                                                            (table5byte16, table5byte17))

    def _immediate_bolus(self, pulse_count, pulse_speed=16, reminders=0, delivery_delay=2, request_msg=""):

        commandBody = struct.pack(">I", 0)
        commandBody += b"\x02"

        bodyForChecksum = b"\x01"
        pulse_span = pulse_speed * pulse_count
        bodyForChecksum += struct.pack(">H", pulse_span)
        bodyForChecksum += struct.pack(">H", pulse_count)
        bodyForChecksum += struct.pack(">H", pulse_count)
        checksum = getChecksum(bodyForChecksum)

        commandBody += struct.pack(">H", checksum)
        commandBody += bodyForChecksum

        msg = self._createMessage(0x1a, commandBody)

        commandBody = bytes([reminders])
        commandBody += struct.pack(">H", pulse_count * 10)
        commandBody += struct.pack(">I", delivery_delay * 100000)
        commandBody += b"\x00\x00\x00\x00\x00\x00"
        msg.addCommand(0x17, commandBody)

        self._sendMessage(msg, with_nonce=True, request_msg=request_msg)

        if self.pod.state_bolus != BolusState.Immediate:
            raise PdmError("Pod did not confirm bolus")

    def _cancelActivity(self, cancelBasal=False, cancelBolus=False, cancelTempBasal=False, beep=False):
        self.logger.debug("Running cancel activity for basal: %s - bolus: %s - tempBasal: %s" % (
                            cancelBasal, cancelBolus, cancelTempBasal))

        commandBody = struct.pack(">I", 0)
        if beep:
            c = 0x60
        else:
            c = 0

        act_str = ""
        if cancelBolus:
            c = c | 0x04
            act_str += "BOLUS "
        if cancelTempBasal:
            c = c | 0x02
            act_str += "TEMPBASAL "
        if cancelBasal:
            c = c | 0x01
            act_str += "BASAL "
        commandBody += bytes([c])

        msg = self._createMessage(0x1f, commandBody)
        self._sendMessage(msg, with_nonce=True, request_msg="CANCEL %s" % act_str)

    def _createMessage(self, commandType, commandBody):
        msg = Message(MessageType.PDM, self.pod.radio_address, sequence=self.get_radio().messageSequence)
        msg.addCommand(commandType, commandBody)
        return msg

    def _savePod(self):
        try:
            self.logger.debug("Saving pod status")
            radio = self.get_radio()
            if radio is not None:
                self.pod.radio_message_sequence = radio.messageSequence
                self.pod.radio_packet_sequence = radio.packetSequence

            nonce = self.get_nonce()
            if nonce is not None:
                self.pod.nonce_last = nonce.lastNonce
                self.pod.nonce_seed = nonce.seed

            self.pod.Save()
            self.logger.debug("Saved pod status")
        except Exception as e:
            raise PdmError("Pod status was not saved") from e

    def _sendMessage(self, message, with_nonce=False, nonce_retry_count=0, request_msg=None, tx_power=None,
                     double_take=False):

        if with_nonce:
            nonce_obj = self.get_nonce()
            if nonce_obj is None:
                raise PdmError("Cannot create nonce for message")
            nonce = nonce_obj.getNext()
            message.setNonce(nonce)

        radio = self.get_radio()

        try:
            response_message = radio.send_request_get_response(message, tx_power=tx_power,
                                                                      double_take=double_take)
        except:
            if with_nonce:
                nonce_obj.reset()
            raise

        contents = response_message.getContents()
        for (ctype, content) in contents:
            if ctype == 0x01:  # pod version response
                 self.pod.handle_version_response(content)
            if ctype == 0x1d:  # status response
                self.pod.handle_status_response(content, original_request=request_msg)
            elif ctype == 0x02:  # pod state_faulted or information
                self.pod.handle_information_response(content, original_request=request_msg)
            elif ctype == 0x06:
                if content[0] == 0x14:  # bad nonce error
                    if nonce_retry_count == 0:
                        self.logger.debug("Bad nonce error - renegotiating")
                    elif nonce_retry_count > 3:
                        raise PdmError("Nonce re-negotiation failed")
                    nonce_sync_word = struct.unpack(">H", content[1:])[0]
                    nonce_obj.sync(nonce_sync_word, message.sequence)
                    radio.messageSequence = message.sequence
                    return self._sendMessage(message, with_nonce=True, nonce_retry_count=nonce_retry_count + 1,
                                             request_msg=request_msg, tx_power=tx_power, double_take=double_take)

    def _update_status(self, update_type=0):
        commandType = 0x0e
        commandBody = bytes([update_type])
        msg = self._createMessage(commandType, commandBody)
        self._sendMessage(msg, request_msg="STATUS REQ %d" % update_type)

    def _acknowledge_alerts(self, alert_mask):
        commandType = 0x11
        commandBody = bytes([0, 0, 0, 0, alert_mask])
        msg = self._createMessage(commandType, commandBody)
        self._sendMessage(msg, with_nonce=True, request_msg="ACK 0x%2X " % alert_mask)

    def _configure_alert(self, alert_bit, activate, trigger_auto_off, duration_minutes, beep_repeat_type, beep_type,
                     alert_after_minutes=None, alert_after_reservoir=None, trigger_reservoir=False):

        if alert_after_minutes is None:
            if alert_after_reservoir is None:
                raise PdmError("Either alert_after_minutes or alert_after_reservoir must be set")
            elif not trigger_reservoir:
                raise PdmError("Trigger insulin_reservoir must be True if alert_after_reservoir is to be set")
        else:
            if alert_after_reservoir is not None:
                raise PdmError("Only one of alert_after_minutes or alert_after_reservoir must be set")
            elif trigger_reservoir:
                raise PdmError("Trigger insulin_reservoir must be False if alert_after_minutes is to be set")

        if duration_minutes > 0x1FF:
            raise PdmError("Alert duration in minutes cannot be more than %d" % 0x1ff)
        elif duration_minutes < 0:
            raise PdmError("Invalid alert duration value")

        if alert_after_minutes is not None and alert_after_minutes > 4800:
            raise PdmError("Alert cannot be set beyond 80 hours")
        if alert_after_minutes is not None and alert_after_minutes < 0:
            raise PdmError("Invalid value for alert_after_minutes")

        if alert_after_reservoir is not None and alert_after_reservoir > 50:
            raise PdmError("Alert cannot be set for more than 50 units")
        if alert_after_reservoir is not None and alert_after_reservoir < 0:
            raise PdmError("Invalid value for alert_after_reservoir")

        b0 = alert_bit << 4
        if activate:
            b0 |= 0x08
        if trigger_reservoir:
            b0 |= 0x04
        if trigger_auto_off:
            b0 |= 0x02

        b0 |= (duration_minutes >> 8) & 0x0001
        b1 = duration_minutes & 0x00ff

        if alert_after_reservoir is not None:
            reservoir_limit = int(alert_after_reservoir * 10)
            b2 = reservoir_limit >> 8
            b3 = reservoir_limit & 0x00ff
        elif alert_after_minutes is not None:
            b2 = alert_after_minutes >> 8
            b3 = alert_after_minutes & 0x00ff
        else:
            raise PdmError("Incorrect alert configuration requested")

        commandType = 0x19
        commandBody = bytes([0, 0, 0, 0, b0, b1, b2, b3, beep_repeat_type, beep_type])

        msg = self._createMessage(commandType, commandBody)
        self._sendMessage(msg, with_nonce=True,
                          request_msg="ACTIVATE ALERT %d: %s" %(alert_bit, activate))

    def _set_basal_schedule(self, schedule, hour=None, minute=None, second=None):

        halved_schedule = []
        two = Decimal("2")

        for entry in schedule:
            halved_schedule.append(entry / two)

        if self.pod.var_utc_offset is None:
            self.pod.var_utc_offset = 0

        utc_offset = timedelta(minutes=self.pod.var_utc_offset)
        pod_date = datetime.utcnow() + utc_offset

        if hour is None:
            hour = pod_date.hour
        if minute is None:
            minute = pod_date.minute
        if second is None:
            second = pod_date.second

        current_hh = hour * 2
        if minute < 30:
            seconds_past_hh = minute * 60
        else:
            seconds_past_hh = (minute - 30) * 60
            current_hh += 1

        seconds_past_hh += second
        seconds_to_hh = 1800 - seconds_past_hh

        pulse_list = getPulsesForHalfHours(halved_schedule)
        ise_list = getInsulinScheduleTableFromPulses(pulse_list)
        ise_body = getStringBodyFromTable(ise_list)
        pulse_body = getStringBodyFromTable(pulse_list)

        command_body = struct.pack(">I", 0)
        command_body += b"\x00"

        body_checksum = bytes([current_hh])

        current_hh_pulse_count = pulse_list[current_hh]
        remaining_pulse_count = int(current_hh_pulse_count * seconds_to_hh / 1800)

        body_checksum += struct.pack(">H", seconds_to_hh * 8)
        body_checksum += struct.pack(">H", remaining_pulse_count)

        checksum = getChecksum(body_checksum + pulse_body)

        command_body += struct.pack(">H", checksum)
        command_body += body_checksum
        command_body += ise_body

        msg = self._createMessage(0x1a, command_body)


        reminders = 0
        # if confidenceReminder:
        #     reminders |= 0x40

        command_body = bytes([reminders])

        command_body += b"\x00"

        pulse_entries = getPulseIntervalEntries(halved_schedule)
        table_index = 0
        for pulses10, interval, indices in pulse_entries:
            if current_hh in indices:
                command_body += bytes([table_index])
                ii = indices.index(current_hh)

                pulses_past_intervals = int(ii * 1800000000 / interval)
                pulses_past_this_interval = int(seconds_past_hh * 1000000 / interval) + 1
                remaining_pulses_this_interval = pulses10 - pulses_past_this_interval - pulses_past_intervals
                microseconds_to_next_interval = interval - (seconds_past_hh * 1000000 % interval)

                command_body += struct.pack(">H", remaining_pulses_this_interval)
                command_body += struct.pack(">I", microseconds_to_next_interval)
                break
            else:
                table_index += 1

        for pulse_count, interval, _ in pulse_entries:
            command_body += struct.pack(">H", pulse_count)
            command_body += struct.pack(">I", interval)

        msg.addCommand(0x13, command_body)

        schedule_str = ""
        for entry in schedule:
            schedule_str += "%2.2f " % entry

        self._sendMessage(msg, with_nonce=True, request_msg="SETBASALSCHEDULE (%s)" % schedule_str,
                          double_take=True)

    def _is_bolus_running(self):
        if self.pod.state_last_updated is not None and self.pod.state_bolus != BolusState.Immediate:
            return False

        if self.pod.last_enacted_bolus_amount is not None \
                and self.pod.last_enacted_bolus_start is not None:

            if self.pod.last_enacted_bolus_amount < 0:
                return False

            now = time.time()
            bolus_end_earliest = (self.pod.last_enacted_bolus_amount * 35) + self.pod.last_enacted_bolus_start
            bolus_end_latest = (self.pod.last_enacted_bolus_amount * 45) + 10 + self.pod.last_enacted_bolus_start
            if now > bolus_end_latest:
                return False
            elif now < bolus_end_earliest:
                return True

        self._update_status()
        return self.pod.state_bolus == BolusState.Immediate

    def _is_basal_schedule_active(self):
        if self.pod.state_last_updated is not None and self.pod.state_basal == BasalState.NotRunning:
            return False

        self._update_status()
        return self.pod.state_basal == BasalState.Program

    def _is_temp_basal_active(self):
        if self.pod.state_last_updated is not None and self.pod.state_basal != BasalState.TempBasal:
            return False

        if self.pod.last_enacted_temp_basal_start is not None \
                and self.pod.last_enacted_temp_basal_duration is not None:
            if self.pod.last_enacted_temp_basal_amount < 0:
                return False
            now = time.time()
            temp_basal_end_earliest = self.pod.last_enacted_temp_basal_start + \
                                      (self.pod.last_enacted_temp_basal_duration * 3600) - 60
            temp_basal_end_latest = self.pod.last_enacted_temp_basal_start + \
                                      (self.pod.last_enacted_temp_basal_duration * 3660) + 60
            if now > temp_basal_end_latest:
                return False
            elif now < temp_basal_end_earliest:
                return True

        self._update_status()
        return self.pod.state_basal == BasalState.TempBasal

    def _assert_pod_activate_can_start(self):
        self._assert_pod_address_not_assigned()

    def _assert_basal_schedule_is_valid(self, schedule):
        if schedule is None:
            raise PdmError("No basal schedule defined")

        if len(schedule) != 48:
            raise PdmError("A full schedule of 48 half hours is needed")

        min_rate = Decimal("0.05")
        max_rate = Decimal("30")

        for entry in schedule:
            if entry < min_rate:
                raise PdmError("A basal rate schedule entry cannot be less than 0.05U/h")
            if entry > max_rate:
                raise PdmError("A basal rate schedule entry cannot be more than 30U/h")

    def _assert_pod_address_not_assigned(self):
        if self.pod is None:
            raise PdmError("No pod instance created")

        if self.pod.radio_address is not None and self.pod.radio_address != 0xffffffff:
            raise PdmError("Radio radio_address already set")

    def _assert_pod_address_assigned(self):
        if self.pod is None:
            raise PdmError("No pod instance created")

        if self.pod.radio_address is None or self.pod.radio_address == 0xffffffff:
            raise PdmError("Radio radio_address not set")

    def _assert_pod_can_activate(self):
        if self.pod is None:
            raise PdmError("No pod instance created")

        if self.pod.id_lot is None:
            raise PdmError("Lot number unknown")

        if self.pod.id_t is None:
            raise PdmError("Serial number unknown")

        if self.pod.state_progress != PodProgress.TankFillCompleted:
            raise PdmError("Pod is not at the expected state of Tank Fill Completed")

    def _assert_pod_paired(self):
        if self.pod.radio_address is None:
            raise PdmError("Radio radio_address not accepted")

        if self.pod.state_progress != PodProgress.PairingSuccess:
            raise PdmError("Progress does not indicate pairing success")

    def _assert_can_deactivate(self):
        self._assert_pod_address_assigned()
        self._assert_can_generate_nonce()
        if self.pod.state_progress < PodProgress.PairingSuccess:
            raise PdmError("Pod is not paired")
        if self.pod.state_progress > PodProgress.AlertExpiredShuttingDown:
            raise PdmError("Pod already deactivated")

    def _assert_can_acknowledge_alerts(self):
        self._assert_pod_address_assigned()
        if self.pod.state_progress < PodProgress.PairingSuccess:
            raise PdmError("Pod not paired completely yet.")

        if self.pod.state_progress == PodProgress.ErrorShuttingDown:
            raise PdmError("Pod is shutting down, cannot acknowledge alerts.")

        if self.pod.state_progress == PodProgress.AlertExpiredShuttingDown:
            raise PdmError("Acknowledgement period expired, pod is shutting down")

        if self.pod.state_progress > PodProgress.AlertExpiredShuttingDown:
            raise PdmError("Pod is not active")

    def _assert_can_generate_nonce(self):
        if self.pod.id_lot is None:
            raise PdmError("Lot number is not defined")

        if self.pod.id_t is None:
            raise PdmError("Pod serial number is not defined")

    def _assert_status_running(self):
        if self.pod.state_progress < PodProgress.Running:
            raise PdmError("Pod is not yet running")

        if self.pod.state_progress > PodProgress.RunningLow:
            raise PdmError("Pod has stopped")

    def _assert_not_faulted(self):
        if self.pod.state_faulted:
            raise PdmError("Pod is state_faulted")

    def _assert_no_active_alerts(self):
        if self.pod.state_alert != 0:
            raise PdmError("Pod has active alerts")

    def _assert_immediate_bolus_not_active(self):
        if self._is_bolus_running():
            raise PdmError("Pod is busy delivering a bolus")


