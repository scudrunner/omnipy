import random


def encodeSingleByte(d):
    e = 0
    for i in range(0, 8):
        e = e << 2
        if d & 0x01 == 0:
            e |= 2
        else:
            e |= 1
        d = d >> 1
    return bytes([(e >> 8), e & 0xff])

class ManchesterCodec:
    def __init__(self):
        self.preamble = bytes([0x66,0x65]) * 200 + bytes([0xa5, 0x5a])
        self.decode_dict = dict()
        self.encode_dict = dict()
        for i in range(0, 256):
            enc = encodeSingleByte(i)
            self.decode_dict[enc] = i
            self.encode_dict[i] = enc

        self.noiseSeq = 0
        noiseNibbles = '0123478bcdef'
        self.noiseLines = []
        for x in range(0, 32):
            noiseLine = "f"
            for i in range(0, 159):
                noiseLine += random.choice(noiseNibbles)
            self.noiseLines.append(bytearray.fromhex(noiseLine))

    def decode(self, data):
        decoded = bytes()
        for i in range(0, len(data), 2):
            word = data[i:i+2]
            if word in self.decode_dict:
                decoded += bytes([self.decode_dict[word]])
            else:
                break
        return decoded

    def encode(self, data):
        encoded = self.preamble
        for i in data:
            encoded += self.encode_dict[i]
        encoded += self.noiseLines[self.noiseSeq]
        self.noiseSeq += 1
        self.noiseSeq %= 32

        minPreamble = 4
        minNoise = 2
        available = 512 - len(data) - minPreamble - minNoise
        dataIndex = len(self.preamble)
        portion = int(available / 2)
        preambleIncluded = minPreamble + portion
        noiseIncluded = minNoise + available - portion
        return encoded[dataIndex - preambleIncluded: dataIndex + noiseIncluded]

