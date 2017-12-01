import array
import codecs
import logging
import os
import struct
import sys
import time
import traceback
from optparse import OptionParser

import kiwiclient

def _write_wav_header(fp, filesize, samplerate, num_channels, is_kiwi_wav):
    fp.write(struct.pack('<4sI4s', 'RIFF', filesize - 8, 'WAVE'))
    bits_per_sample = 16
    byte_rate       = samplerate * num_channels * bits_per_sample / 8
    block_align     = num_channels * bits_per_sample / 8
    fp.write(struct.pack('<4sIHHIIHH', 'fmt ', 16, 1, num_channels, samplerate, byte_rate, block_align, bits_per_sample))
    if not is_kiwi_wav:
        fp.write(struct.pack('<4sI', 'data', filesize - 12 - 8 - 16 - 8))

class KiwiRecorder(kiwiclient.KiwiSDRSoundStream):
    def __init__(self, options, server):
        super(KiwiRecorder, self).__init__()
        self._options = options
        self._server = server
        freq = options.frequency
        if server == 1 and options.frequency2:
            freq = options.frequency2
        #print "%s:%s freq=%d" % (options.server_host, options.server_port, freq)
        self._freq = freq
        self._start_ts = None
        self._squelch_on_seq = None
        self._nf_array = array.array('i')
        for x in xrange(65):
            self._nf_array.insert(x, 0)
        self._nf_samples = 0
        self._nf_index = 0
        self._num_channels = 2 if options.modulation == 'iq' else 1
        self._last_gps = dict(zip(['last_gps_solution', 'dummy', 'gpssec', 'gpsnsec'], [0,0,0,0]))

    def _setup_rx_params(self):
        mod    = self._options.modulation
        lp_cut = self._options.lp_cut
        hp_cut = self._options.hp_cut
        if (mod == 'am'):
            # For AM, ignore the low pass filter cutoff
            lp_cut = -hp_cut
        self.set_mod(mod, lp_cut, hp_cut, self._freq)
        if self._options.agc_off:
            self.set_agc(on=False, gain=self._options.agc_gain)
        else:
            self.set_agc(on=True)
        self.set_inactivity_timeout(0)
        self.set_name('kiwirecorder.py')
        # self.set_geo('Antarctica')

    def _process_audio_samples(self, seq, samples, rssi):
        sys.stdout.write('\rBlock: %08x, RSSI: %-04d' % (seq, rssi))
        if self._nf_samples < len(self._nf_array) or self._squelch_on_seq is None:
            self._nf_array[self._nf_index] = rssi
            self._nf_index += 1
            if self._nf_index == len(self._nf_array):
                self._nf_index = 0
        if self._nf_samples < len(self._nf_array):
            self._nf_samples += 1
            return

        median_nf = sorted(self._nf_array)[len(self._nf_array) // 3]
        rssi_thresh = median_nf + self._options.thresh
        is_open = self._squelch_on_seq is not None
        if is_open:
            rssi_thresh -= 6
        rssi_green = rssi >= rssi_thresh
        if rssi_green:
            self._squelch_on_seq = seq
            is_open = True
        sys.stdout.write(' Median: %-04d Thr: %-04d %s' % (median_nf, rssi_thresh, ("s", "S")[is_open]))
        if not is_open:
            return
        if seq > self._squelch_on_seq + 45:
            print "\nSquelch closed"
            self._squelch_on_seq = None
            self._start_ts = None
            return
        self._write_samples(samples, {})

    def _process_iq_samples(self, seq, samples, rssi, gps):
        #sys.stdout.write('\rBlock: %08x, RSSI: %-04d' % (seq, rssi))
        #sys.stdout.flush()
        ## convert list of complex numbers to an array
        ##print gps['gpsnsec']-self._last_gps['gpsnsec']
        self._last_gps = gps
        s = array.array('h')
        for x in [[y.real, y.imag] for y in samples]:
            s.extend(map(int, x))
        self._write_samples(s, gps)

    def _get_output_filename(self):
        ts = time.strftime('%Y%m%dT%H%M%SZ', self._start_ts)
        sta = '' if self._options.station is None else '_' + (self._options.station2 if self._server == 1 and self._options.station2 != None else self._options.station)
        server = '' if self._options.two_servers == False else '_server' + str(self._server)
        return '%s_%d_%s%s%s.wav' % (ts, int(self._freq * 1000), self._options.modulation, sta, server)

    def _update_wav_header(self):
        with open(self._get_output_filename(), 'r+b') as fp:
            fp.seek(0, os.SEEK_END)
            filesize = fp.tell()
            fp.seek(0, os.SEEK_SET)
            _write_wav_header(fp, filesize, int(self._sample_rate), self._num_channels, self._options.is_kiwi_wav)

    def _write_samples(self, samples, *args):
        """Output to a file on the disk."""
        # print '_write_samples', args
        now = time.gmtime()
        if self._start_ts is None or self._start_ts.tm_hour != now.tm_hour:
            self._start_ts = now
            # Write a static WAV header
            with open(self._get_output_filename(), 'wb') as fp:
                _write_wav_header(fp, 100, int(self._sample_rate), self._num_channels, self._options.is_kiwi_wav)
            print "\nStarted a new file: %s" % (self._get_output_filename())
        with open(self._get_output_filename(), 'ab') as fp:
            if (self._options.is_kiwi_wav):
                gps = args[0]
                sys.stdout.write('gpssec: %d' % (gps['gpssec']))
                fp.write(struct.pack('<4sIBBII', 'kiwi', 10, gps['last_gps_solution'], 0, gps['gpssec'], gps['gpsnsec']))
                sample_size = samples.itemsize * len(samples)
                fp.write(struct.pack('<4sI', 'data', sample_size))
            # TODO: something better than that
            samples.tofile(fp)
        self._update_wav_header()


def main():
    sys.stdout = codecs.getwriter('utf-8')(sys.stdout)

    parser = OptionParser()
    parser.add_option('--log-level', '--log_level', type='choice',
                      dest='log_level', default='warn',
                      choices=['debug', 'info', 'warn', 'error', 'critical'],
                      help='Log level.')
    parser.add_option('-k', '--socket-timeout', '--socket_timeout',
                      dest='socket_timeout', type='int', default=10,
                      help='Timeout(sec) for sockets')
    parser.add_option('-s', '--s1', '--server-host', '--server_host',
                      dest='server_host', type='string',
                      default='localhost', help='server host')
    parser.add_option('-p', '--p1', '--server-port', '--server_port',
                      dest='server_port', type='int',
                      default=8073, help='server port (default 8073)')

    parser.add_option('-2', '--2servers',
                      dest='two_servers', action='store_true',
                      default=False, help='Connect to two servers simultaneously.')
    parser.add_option('--s2', '--server-host2', '--server_host2',
                      dest='server_host2', type='string',
                      default='localhost', help='server host2')
    parser.add_option('--p2', '--server-port2', '--server_port2',
                      dest='server_port2', type='int',
                      default=8073, help='server port2 (default 8073)')

    parser.add_option('-f', '--freq', '--f1', '--freq1',
                      dest='frequency',
                      type='float', default=1000,
                      help='Frequency to tune to, in kHz.')
    parser.add_option('--f2', '--freq2',
                      dest='frequency2',
                      type='float', default=0,
                      help='Optional frequency for second server to tune to, in kHz.')
    parser.add_option('-m', '--modulation',
                      dest='modulation',
                      type='string', default='am',
                      help='Modulation; one of am, lsb, usb, cw, nbfm, iq')
    parser.add_option('-L', '--lp-cutoff',
                      dest='lp_cut',
                      type='float', default=100,
                      help='Low-pass cutoff frequency, in Hz.')
    parser.add_option('-H', '--hp-cutoff',
                      dest='hp_cut',
                      type='float', default=2600,
                      help='Low-pass cutoff frequency, in Hz.')
    parser.add_option('--station',
                      dest='station',
                      type='string', default=None,
                      help='Station ID to be appended.')
    parser.add_option('--station2',
                      dest='station2',
                      type='string', default=None,
                      help='Station ID to be appended for second server.')
    parser.add_option('-T', '--threshold',
                      dest='thresh',
                      type='float', default=0,
                      help='Squelch threshold, in dB.')
    parser.add_option('-w', '--kiwi-wav',
                      dest='is_kiwi_wav',
                      default=False,
                      action='store_true',
                      help='wav file format including KIWI header (only for IQ mode)')
    parser.add_option('-a', '--agc-off',
                      dest='agc_off',
                      default=False,
                      action='store_true',
                      help='AGC OFF (default gain=40)')
    parser.add_option('-g', '--agc-gain',
                      dest='agc_gain',
                      type='float',
                      default=40,
                      help='AGC gain.')

    (options, unused_args) = parser.parse_args()

    logging.basicConfig(level=logging.getLevelName(options.log_level.upper()))

    while True:
        recorder = KiwiRecorder(options, 0)
        if options.two_servers:
            # print "recorder2"
            recorder2 = KiwiRecorder(options, 1)

        # Connect
        try:
            recorder.connect(options.server_host, options.server_port)
            if options.two_servers:
                recorder2.connect(options.server_host2, options.server_port2)
        except KeyboardInterrupt:
            break
        except:
            print "Failed to connect, sleeping and reconnecting"
            time.sleep(15)
            continue

        # Open
        try:
            recorder.open()
            if options.two_servers:
                recorder2.open()
        except KeyboardInterrupt:
            break
        except kiwiclient.KiwiTooBusyError:
            print "Server too busy now"
            time.sleep(15)
            continue
        except Exception as e:
            traceback.print_exc()
            break

        # Record
        try:
            while True:
                recorder.run()
                if options.two_servers:
                    recorder2.run()
        except KeyboardInterrupt:
            break
        except kiwiclient.KiwiTooBusyError:
            print "Server too busy now"
            time.sleep(15)
            break
        except Exception as e:
            traceback.print_exc()
            break

    # Close
    recorder.close()
    if options.two_servers:
        recorder2.close()
    print "exiting"


if __name__ == '__main__':
    main()

# EOF
