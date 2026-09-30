from aiortc import RTCPeerConnection
from aiortc.jitterbuffer import JitterBuffer
from aiortc.rtp import RtpPacket

from services.realtime.receive_buffer import AudioReceiveBuffer, configure_audio_receivers


def packet(index, *, ssrc=1, timestamp=None):
    p = RtpPacket(sequence_number=index & 0xFFFF,
                  timestamp=(index * 960 if timestamp is None else timestamp) & 0xFFFFFFFF,
                  ssrc=ssrc)
    p._data = str(index).encode()
    return p


def test_loss_does_not_leave_the_rest_of_the_call_300ms_behind():
    buffer = AudioReceiveBuffer()
    baseline = JitterBuffer(capacity=16, prefetch=4)
    delivered, lag, old_lag = [], [], []
    missing = {12, 32, 55, 56, 57, 81}
    for index in range(110):
        if index in missing:
            continue
        _, old = baseline.add(packet(index))
        _, frame = buffer.add(packet(index))
        if old:
            old_lag.append(index - old.timestamp // 960)
        if frame:
            delivered.append(int(frame.data))
            lag.append(index - frame.timestamp // 960)
    assert max(old_lag) >= 14  # reproduce the dependency's persistent latency
    assert max(lag) <= 6  # three missing arrivals can briefly delay valid frames
    assert lag[-20:] == [3] * 20  # no persistent latency after loss
    assert delivered == [i for i in range(107) if i not in missing]
    assert buffer.skipped_packets == len(missing)
    assert buffer.max_pending <= 4


def test_reorders_without_losing_valid_audio_and_ignores_duplicates():
    buffer = AudioReceiveBuffer()
    frames = []
    for index in [0, 2, 1, 3, 3, 5, 4, 6, 8, 7, 9]:
        _, frame = buffer.add(packet(index))
        if frame:
            frames.append(int(frame.data))
    assert frames == list(range(7))
    assert buffer.skipped_packets == 0


def test_sequence_wrap_and_dtx_timestamp_jump_keep_packet_order():
    buffer = AudioReceiveBuffer()
    frames = []
    for index in range(65533, 65550):
        _, frame = buffer.add(packet(index, timestamp=0xFFFFFF00 + index * 960 + (480000 if index > 65536 else 0)))
        if frame:
            frames.append(int(frame.data))
    assert frames == list(range(65533, 65547))
    assert buffer.skipped_packets == 0


def test_late_packets_are_not_replayed_and_long_outage_is_bounded():
    buffer = AudioReceiveBuffer()
    for index in range(10):
        buffer.add(packet(index))
    assert buffer.add(packet(0)) == (False, None)
    assert buffer.late_packets == 1
    for index in range(1000, 1004):
        _, frame = buffer.add(packet(index))
    assert frame.data == b"1000"
    assert len(buffer._packets) == 3
    assert buffer.add(packet(0, ssrc=2)) == (False, None)
    assert len(buffer._packets) == 1


def test_installs_only_on_audio_receivers_and_is_idempotent():
    pc = RTCPeerConnection()
    audio = pc.addTransceiver("audio")
    video = pc.addTransceiver("video")
    video_buffer = video.receiver._RTCRtpReceiver__jitter_buffer
    buffers = configure_audio_receivers(pc)
    assert audio.receiver._RTCRtpReceiver__jitter_buffer is buffers[0]
    assert video.receiver._RTCRtpReceiver__jitter_buffer is video_buffer
    assert configure_audio_receivers(pc)[0] is buffers[0]
