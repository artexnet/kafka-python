# -*- coding: utf-8 -*-

import time
import logging

from mock import MagicMock, patch
from . import unittest

from kafka.common import TopicAndPartition, FailedPayloadsError, RetryOptions
from kafka.common import AsyncProducerQueueFull
from kafka.producer.base import Producer
from kafka.producer.base import _send_upstream
from kafka.protocol import CODEC_NONE

import threading
try:
    from queue import Empty, Queue
except ImportError:
    from Queue import Empty, Queue
try:
    xrange
except NameError:
    xrange = range


class TestKafkaProducer(unittest.TestCase):
    def test_producer_message_types(self):

        producer = Producer(MagicMock())
        topic = b"test-topic"
        partition = 0

        bad_data_types = (u'你怎么样?', 12, ['a', 'list'], ('a', 'tuple'), {'a': 'dict'})
        for m in bad_data_types:
            with self.assertRaises(TypeError):
                logging.debug("attempting to send message of type %s", type(m))
                producer.send_messages(topic, partition, m)

        good_data_types = (b'a string!',)
        for m in good_data_types:
            # This should not raise an exception
            producer.send_messages(topic, partition, m)

    def test_topic_message_types(self):
        from kafka.producer.simple import SimpleProducer

        client = MagicMock()

        def partitions(topic):
            return [0, 1]

        client.get_partition_ids_for_topic = partitions

        producer = SimpleProducer(client, random_start=False)
        topic = b"test-topic"
        producer.send_messages(topic, b'hi')
        assert client.send_produce_request.called

    @patch('kafka.producer.base._send_upstream')
    def test_producer_async_queue_overfilled(self, mock):
        queue_size = 2
        producer = Producer(MagicMock(), async=True,
                            async_queue_maxsize=queue_size)

        topic = b'test-topic'
        partition = 0
        message = b'test-message'

        with self.assertRaises(AsyncProducerQueueFull):
            message_list = [message] * (queue_size + 1)
            producer.send_messages(topic, partition, *message_list)
        self.assertEqual(producer.queue.qsize(), queue_size)
        for _ in xrange(producer.queue.qsize()):
            producer.queue.get()


class TestKafkaProducerSendUpstream(unittest.TestCase):

    def setUp(self):
        self.client = MagicMock()
        self.queue = Queue()

    def _run_process(self, retries_limit=3, sleep_timeout=1):
        # run _send_upstream process with the queue
        stop_event = threading.Event()
        retry_options = RetryOptions(limit=retries_limit,
                                     backoff_ms=50,
                                     retry_on_timeouts=False)
        self.thread = threading.Thread(
            target=_send_upstream,
            args=(self.queue, self.client, CODEC_NONE,
                  0.3, # batch time (seconds)
                  3, # batch length
                  Producer.ACK_AFTER_LOCAL_WRITE,
                  Producer.DEFAULT_ACK_TIMEOUT,
                  retry_options,
                  stop_event))
        self.thread.daemon = True
        self.thread.start()
        time.sleep(sleep_timeout)
        stop_event.set()

    def test_wo_retries(self):

        # lets create a queue and add 10 messages for 1 partition
        for i in range(10):
            self.queue.put((TopicAndPartition("test", 0), "msg %i", "key %i"))

        self._run_process()

        # the queue should be void at the end of the test
        self.assertEqual(self.queue.empty(), True)

        # there should be 4 non-void cals:
        # 3 batches of 3 msgs each + 1 batch of 1 message
        self.assertEqual(self.client.send_produce_request.call_count, 4)

    def test_first_send_failed(self):

        # lets create a queue and add 10 messages for 10 different partitions
        # to show how retries should work ideally
        for i in range(10):
            self.queue.put((TopicAndPartition("test", i), "msg %i", "key %i"))

        self.client.is_first_time = True
        def send_side_effect(reqs, *args, **kwargs):
            if self.client.is_first_time:
                self.client.is_first_time = False
                return [FailedPayloadsError(req) for req in reqs]
            return []

        self.client.send_produce_request.side_effect = send_side_effect

        self._run_process(2)

        # the queue should be void at the end of the test
        self.assertEqual(self.queue.empty(), True)

        # there should be 5 non-void cals: 1st failed batch of 3 msgs
        # + 3 batches of 3 msgs each + 1 batch of 1 msg = 1 + 3 + 1 = 5
        self.assertEqual(self.client.send_produce_request.call_count, 5)

    def test_with_limited_retries(self):

        # lets create a queue and add 10 messages for 10 different partitions
        # to show how retries should work ideally
        for i in range(10):
            self.queue.put((TopicAndPartition("test", i), "msg %i" % i, "key %i" % i))

        def send_side_effect(reqs, *args, **kwargs):
            return [FailedPayloadsError(req) for req in reqs]

        self.client.send_produce_request.side_effect = send_side_effect

        self._run_process(3, 3)

        # the queue should be void at the end of the test
        self.assertEqual(self.queue.empty(), True)

        # there should be 16 non-void cals:
        # 3 initial batches of 3 msgs each + 1 initial batch of 1 msg +
        # 3 retries of the batches above = 4 + 3 * 4 = 16, all failed
        self.assertEqual(self.client.send_produce_request.call_count, 16)

    def tearDown(self):
        for _ in xrange(self.queue.qsize()):
            self.queue.get()
