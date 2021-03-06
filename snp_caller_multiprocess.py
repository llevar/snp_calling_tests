import pysam
from math import log10
from kafka import KafkaConsumer
import json
import jsonpickle
import redis
from model.locus import Locus
from multiprocessing import Process, Pipe, Manager
from apscheduler.schedulers.background import BackgroundScheduler
import time
import datetime

redis_cli = redis.Redis(host='localhost',port=6379)

SAVED_MSG = "SAVED"
NEW_MSG = "NEW"
USE_REDIS = False
all_loci = {}
#rev_comp_lookup = string.maketrans(u'ACTG', u'TGAC')
rev_comp_lookup = {ord(c): ord(t) for c,t in zip(u'ACTG', u'TGAC')}
def comp(seq):
    return seq.translate(rev_comp_lookup)

def rev_comp(seq):
    return comp(seq)[::-1]

def to_phred(x):
    return -10*(log10(x))

def from_phred(x):
    return pow(10, -x/10)

def ascii_to_qual(qual_str):
    return map(lambda(x): ord(x) - 33, qual_str)

def get_priors():
    heterozygosity = 0.8 * 0.001
    prior_1 = heterozygosity
    prior_0 = pow(heterozygosity, 2)
    prior_2 = 1 - prior_1 - prior_0
    return (prior_0, prior_1, prior_2)

(prior_0, prior_1, prior_2) = get_priors()

def min_gl_if_zero(val):
    if val == 0:
        return Locus.MIN_GL
    else:
        return val

def timeit(method):
    def timed(*args, **kw):
        ts = time.time()
        result = method(*args, **kw)
        te = time.time()
        if 'log_time' in kw:
            name = kw.get('log_name', method.__name__.upper())
            kw['log_time'][name] = int((te - ts) * 1000)
        else:
            print '%r  %2.2f ms\n' % (method.__name__, (te - ts) * 1000)
        return result
    return timed



def handle_match(read, read_idx, match_len, ref_offset, ref, my_start, my_stop, shared_all_loci):

    strand = read['strand']

    seq = read['seq'][read_idx:read_idx + match_len]
    qual = ascii_to_qual(read['qual'])[read_idx:read_idx + match_len]

    updated_loci = {}

    if strand == 1:
        ref_start = read['pos'] + ref_offset
    else:
        seq = rev_comp(seq)
        qual = ascii_to_qual(read['qual'])[read_idx:read_idx+match_len]
        ref_start = read['end'] + ref_offset - match_len

    ref_stop = ref_start + match_len



    if ref_stop < my_start:
        return
    else:
        read_ref = ref[ref_start:ref_stop]

        for i in range(len(seq)):
            cur_ref = ref_start + i

            if cur_ref < my_start:
                continue
            elif cur_ref > my_stop:
                return
            else:
                if USE_REDIS:
                    my_locus_redis = redis_cli.get(cur_ref)
                    if not my_locus_redis:
                        my_locus = Locus(pos=cur_ref + 1, ref=ref[cur_ref], alt=None, gl_ref=prior_2, gl_het=prior_1,
                                         gl_hom=prior_0, history=[])
                    else:
                        my_locus = jsonpickle.decode(my_locus_redis, classes=Locus)
                else:
                    my_locus = all_loci.get(cur_ref)
                    if not my_locus:
                        my_locus = Locus(pos=cur_ref + 1, ref=ref[cur_ref - my_start], alt=None, gl_ref=prior_2,
                                         gl_het=prior_1, gl_hom=prior_0, history=[])

                p_error = from_phred(qual[i])

                my_locus.dp += 1

                my_locus.gl_het *= 0.5

                if (seq[i] != ref[cur_ref - my_start]):
                    my_locus.alt = seq[i]
                    my_locus.gl_ref *= p_error

                    my_locus.gl_hom *= (1 - p_error)

                    my_locus.ao += 1
                    my_locus.qa += qual[i]

                else:
                    my_locus.gl_ref *= (1 - p_error)
                    my_locus.gl_hom *= p_error
                    my_locus.ro += 1
                    my_locus.qr += qual[i]

                # my_locus.add_to_history(my_locus.gl_ref)

                my_locus.update_gls_to_min_if_zero()

                if USE_REDIS:
                    redis_cli.set(cur_ref, jsonpickle.encode(my_locus))
                else:
                    all_loci[cur_ref] = my_locus
                    #shared_all_loci[cur_ref] = my_locus
                    updated_loci[cur_ref] = my_locus
    return updated_loci

def process_read(read, ref, my_start, my_stop, shared_all_loci):
    updated_loci = {}

    strand = read['strand']

    cigar_els = read['cigartuples'] if strand == 1 else read['cigartuples'][::-1]

    if not cigar_els:
        print("Read {} has bad CIGAR {}, skipping".format(read.qname, read.cigar))
        return


    ref_offset = 0

    read_idx = read['qstart']

    for el in cigar_els:
        el_type = el[1]
        el_len = el[0]

        #0 is a match, 2 is a deletion, these consume reference sequence
        if el_type == 0 or el_type == 2:
            if el_type == 0:
                updated_loci.update(handle_match(read, read_idx, el_len, ref_offset, ref, my_start, my_stop, shared_all_loci))
            ref_offset += el_len * strand
        if el_type != 2 and el_type != 4:
            read_idx += el_len

    return updated_loci

def reverse_neg_strand_read(read):
    return True


@timeit
def save_to_redis(saver_pipe, shared_all_loci):
    print("Starting saving to Redis at {}".format(datetime.datetime.now()))

    if saver_pipe.poll():

        msg = saver_pipe.recv()
        print(msg)
        if msg == NEW_MSG:
            redis_cli = redis.Redis(host='localhost', port=6379)
            for key in shared_all_loci.keys():
                my_locus = shared_all_loci[key]
                redis_cli.set(my_locus.pos - 1, jsonpickle.encode(my_locus))

                saver_pipe.send(SAVED_MSG)
        else:
            print("No new messages reported. Not saving to Redis this time.")
    else:
        print("No messages from process. Not saving to Redis this time.")

def save_job(saver_pipe, shared_all_loci):
    p = Process(target=save_to_redis, args=(saver_pipe, shared_all_loci))
    p.start()
    p.join()


def process_messages(processor_pipe, shared_all_loci):
    print("Starting Message Processing")
    consumer = KafkaConsumer('mapped_reads',
                             group_id='rheos_common',
                             bootstrap_servers=['localhost:9092'],
                             value_deserializer=lambda m: json.loads(m.decode('utf-8')))

    start_time = time.time()
    updated_loci = {}

    reference_file = "/Users/siakhnin/data/reference/genome.fa"
    ref = pysam.FastaFile(reference_file).fetch(region="20")
    reads_list = []
    counter = 1
    saver_notified = False

    for message in consumer:
        # message value and key are raw bytes -- decode if necessary!
        # e.g., for unicode: `message.value.decode('utf-8')`
        #print ("%s:%d:%d: key=%s value=%s" % (message.topic, message.partition,
        #                                      message.offset, message.key,
        #                                      message.value))

        if processor_pipe.poll():
            msg = processor_pipe.recv()
            if msg == SAVED_MSG:
                saver_notified = False

        if not saver_notified:
            processor_pipe.send(NEW_MSG)
            saver_notified = True
            print("Sending message to saver")

        my_read = message.value

        updated_loci.update(process_read(my_read, ref, 0, len(ref), shared_all_loci))

        if counter % 1000 == 0:
            print("Processed {} messages. Updating shared dictionary".format(counter))
            ts = time.time()
            shared_all_loci.update(updated_loci)
            te = time.time()
            print("Took {} to update {} loci".format(te - ts, len(updated_loci)))
            updated_loci = {}
            #break
        counter+=1


processor_pipe, saver_pipe = Pipe(duplex=True)
manager = Manager()
shared_all_loci = manager.dict()
scheduler = BackgroundScheduler()
job = scheduler.add_job(save_job, 'interval', seconds=60, args=(saver_pipe, shared_all_loci))
scheduler.start()

#process_messages()
#cProfile.run("process_messages()")

p = Process(target=process_messages, args=(processor_pipe, shared_all_loci))
p.start()
p.join()

# reads = msgpack.loads(reads_9999999_10001000_pack)
# for my_read in reads:
#     process_read(my_read, all_loci, ref, 0, len(ref))
