"""Implementation of the decoder of the Transformer model.

Described in Vaswani et al. (2017), arxiv.org/abs/1706.03762
"""
from typing import Callable, Set, List, Tuple  # pylint: disable=unused-import
import math

import tensorflow as tf
from typeguard import check_argument_types

from neuralmonkey.attention.scaled_dot_product import (
    #empty_multi_head_loop_state
    attention)
from neuralmonkey.attention.base_attention import (
    Attendable, get_attention_states, get_attention_mask)
from neuralmonkey.decorators import tensor
from neuralmonkey.decoders.autoregressive import (
    AutoregressiveDecoder, LoopState, extend_namedtuple, DecoderHistories,
    DecoderFeedables)
from neuralmonkey.encoders.transformer import (
    TransformerLayer, position_signal)
from neuralmonkey.model.sequence import EmbeddedSequence
from neuralmonkey.logging import log
from neuralmonkey.nn.utils import dropout
from neuralmonkey.vocabulary import (
    Vocabulary, PAD_TOKEN_INDEX, END_TOKEN_INDEX)
from neuralmonkey.tf_utils import layer_norm

# pylint: disable=invalid-name
# TODO: handle attention histories
TransformerHistories = extend_namedtuple(
    "TransformerHistories",
    DecoderHistories,
    [("decoded_symbols", tf.Tensor),
     #("self_attention_histories", List[Tuple]),
     #("inter_attention_histories", List[Tuple]),
     ("input_mask", tf.Tensor)])
# pylint: enable=invalid-name


class TransformerDecoder(AutoregressiveDecoder):

    # pylint: disable=too-many-arguments,too-many-locals
    def __init__(self,
                 name: str,
                 encoder: Attendable,
                 vocabulary: Vocabulary,
                 data_id: str,
                 # TODO infer the default for these three from the encoder
                 ff_hidden_size: int,
                 n_heads_self: int,
                 n_heads_enc: int,
                 depth: int,
                 max_output_len: int,
                 dropout_keep_prob: float = 1.0,
                 embedding_size: int = None,
                 embeddings_source: EmbeddedSequence = None,
                 tie_embeddings: bool = True,
                 label_smoothing: float = None,
                 attention_dropout_keep_prob: float = 1.0,
                 use_att_transform_bias: bool = False,
                 supress_unk: bool = False,
                 save_checkpoint: str = None,
                 load_checkpoint: str = None) -> None:
        """Create a decoder of the Transformer model.

        Described in Vaswani et al. (2017), arxiv.org/abs/1706.03762

        Arguments:
            encoder: Input encoder of the decoder.
            vocabulary: Target vocabulary.
            data_id: Target data series.
            name: Name of the decoder. Should be unique accross all Neural
                Monkey objects.
            max_output_len: Maximum length of an output sequence.
            dropout_keep_prob: Probability of keeping a value during dropout.
            embedding_size: Size of embedding vectors for target words.
            embeddings_source: Embedded sequence to take embeddings from.
            tie_embeddings: Use decoder.embedding_matrix also in place
                of the output decoding matrix.

        Keyword arguments:
            ff_hidden_size: Size of the feedforward sublayers.
            n_heads_self: Number of the self-attention heads.
            n_heads_enc: Number of the attention heads over the encoder.
            depth: Number of sublayers.
            label_smoothing: A label smoothing parameter for cross entropy
                loss computation.
            attention_dropout_keep_prob: Probability of keeping a value
                during dropout on the attention output.
            supress_unk: If true, decoder will not produce symbols for unknown
                tokens.
        """
        check_argument_types()
        AutoregressiveDecoder.__init__(
            self,
            name=name,
            vocabulary=vocabulary,
            data_id=data_id,
            max_output_len=max_output_len,
            dropout_keep_prob=dropout_keep_prob,
            embedding_size=embedding_size,
            embeddings_source=embeddings_source,
            tie_embeddings=tie_embeddings,
            label_smoothing=label_smoothing,
            supress_unk=supress_unk,
            save_checkpoint=save_checkpoint,
            load_checkpoint=load_checkpoint)

        self.encoder = encoder
        self.ff_hidden_size = ff_hidden_size
        self.n_heads_self = n_heads_self
        self.n_heads_enc = n_heads_enc
        self.depth = depth
        self.attention_dropout_keep_prob = attention_dropout_keep_prob
        self.use_att_transform_bias = use_att_transform_bias

        self.encoder_states = get_attention_states(self.encoder)
        self.encoder_mask = get_attention_mask(self.encoder)
        self.dimension = \
            self.encoder_states.get_shape()[2].value  # type: ignore

        if self.embedding_size != self.dimension:
            raise ValueError("Model dimension and input embedding size"
                             "do not match")

        log("Decoder cost op: {}".format(self.cost))
        self._variable_scope.reuse_variables()
        log("Runtime logits: {}".format(self.runtime_logits))
    # pylint: enable=too-many-arguments,too-many-locals

    @property
    def output_dimension(self) -> int:
        return self.dimension

    def embed_inputs(self, inputs: tf.Tensor) -> tf.Tensor:
        embedded = tf.nn.embedding_lookup(self.embedding_matrix, inputs)

        if (self.embeddings_source is not None
                and self.embeddings_source.scale_embeddings_by_depth):

            # Pylint @property-related bug
            # pylint: disable=no-member
            embedding_size = self.embedding_matrix.shape.as_list()[-1]
            # pylint: enable=no-member

            embedded *= math.sqrt(embedding_size)

        length = tf.shape(inputs)[1]
        return embedded + position_signal(self.dimension, length)

    @tensor
    def embedded_train_inputs(self) -> tf.Tensor:
        # THE LAST TRAIN INPUT IS NOT USED IN DECODING FUNCTION
        # (just as a target)

        # shape (batch, 1 + (time - 1))
        input_tokens = tf.concat(
            [tf.expand_dims(self.go_symbols, 1),
             tf.transpose(self.train_inputs[:-1])], 1)

        input_embeddings = self.embed_inputs(input_tokens)

        return dropout(input_embeddings,
                       self.dropout_keep_prob,
                       self.train_mode)

    def self_attention_sublayer(
            self, prev_layer: TransformerLayer) -> tf.Tensor:
        """Create the decoder self-attention sublayer with output mask."""

        # Layer normalization
        normalized_states = layer_norm(prev_layer.temporal_states)

        # Run self-attention
        # TODO handle attention histories
        self_context, _ = attention(
            queries=normalized_states,
            keys=normalized_states,
            values=normalized_states,
            keys_mask=prev_layer.temporal_mask,
            num_heads=self.n_heads_self,
            masked=True,
            dropout_callback=lambda x: dropout(
                x, self.attention_dropout_keep_prob, self.train_mode),
            use_bias=self.use_att_transform_bias)

        # Apply dropout
        self_context = dropout(
            self_context, self.dropout_keep_prob, self.train_mode)

        # Add residual connections
        return self_context + prev_layer.temporal_states

    def encoder_attention_sublayer(self, queries: tf.Tensor) -> tf.Tensor:
        """Create the encoder-decoder attention sublayer."""

        # Layer normalization
        normalized_queries = layer_norm(queries)

        # Attend to the encoder
        # TODO handle attention histories
        encoder_context, _ = attention(
            queries=normalized_queries,
            keys=self.encoder_states,
            values=self.encoder_states,
            keys_mask=self.encoder_mask,
            num_heads=self.n_heads_enc,
            dropout_callback=lambda x: dropout(
                x, self.attention_dropout_keep_prob, self.train_mode),
            use_bias=self.use_att_transform_bias)

        # Apply dropout
        encoder_context = dropout(
            encoder_context, self.dropout_keep_prob, self.train_mode)

        # Add residual connections
        return encoder_context + queries

    def feedforward_sublayer(self, layer_input: tf.Tensor) -> tf.Tensor:
        """Create the feed-forward network sublayer."""

        # Layer normalization
        normalized_input = layer_norm(layer_input)

        # Feed-forward network hidden layer + ReLU
        ff_hidden = tf.layers.dense(
            normalized_input, self.ff_hidden_size, activation=tf.nn.relu,
            name="hidden_state")

        # Apply dropout on the activations
        ff_hidden = dropout(ff_hidden, self.dropout_keep_prob, self.train_mode)

        # Feed-forward output projection
        ff_output = tf.layers.dense(ff_hidden, self.dimension, name="output")

        # Apply dropout on the output projection
        ff_output = dropout(ff_output, self.dropout_keep_prob, self.train_mode)

        # Add residual connections
        return ff_output + layer_input

    def layer(self, level: int, inputs: tf.Tensor,
              mask: tf.Tensor) -> TransformerLayer:
        # Recursive implementation. Outputs of the zeroth layer
        # are the inputs

        if level == 0:
            return TransformerLayer(inputs, mask)

        # Compute the outputs of the previous layer
        prev_layer = self.layer(level - 1, inputs, mask)

        with tf.variable_scope("layer_{}".format(level - 1)):

            with tf.variable_scope("self_attention"):
                self_context = self.self_attention_sublayer(prev_layer)

            with tf.variable_scope("encdec_attention"):
                encoder_context = self.encoder_attention_sublayer(self_context)

            with tf.variable_scope("feedforward"):
                output_states = self.feedforward_sublayer(encoder_context)

        # Layer normalization on the decoder output
        if self.depth == level:
            output_states = layer_norm(output_states)

        return TransformerLayer(states=output_states, mask=mask)

    @tensor
    def train_logits(self) -> tf.Tensor:
        last_layer = self.layer(self.depth, self.embedded_train_inputs,
                                tf.transpose(self.train_mask))

        # t_states shape: (batch, time, channels)
        # dec_w shape: (channels, vocab)
        last_layer_shape = tf.shape(last_layer.temporal_states)
        last_layer_states = tf.reshape(
            last_layer.temporal_states,
            [-1, last_layer_shape[-1]])

        # Reusing input embedding matrix for generating logits
        # significantly reduces the overall size of the model.
        # See: https://arxiv.org/pdf/1608.05859.pdf
        #
        # shape (batch, time, vocab)
        logits = tf.reshape(
            tf.matmul(last_layer_states, self.decoding_w),
            [last_layer_shape[0], last_layer_shape[1], len(self.vocabulary)])
        logits += tf.reshape(self.decoding_b, [1, 1, -1])

        # return logits in time-major shape
        return tf.transpose(logits, perm=[1, 0, 2])

    def get_initial_loop_state(self) -> LoopState:

        default_ls = AutoregressiveDecoder.get_initial_loop_state(self)
        histories = default_ls.histories._asdict()

#        histories["self_attention_histories"] = [
#            empty_multi_head_loop_state(self.batch_size, self.n_heads_self)
#            for a in range(self.depth)]

#        histories["inter_attention_histories"] = [
#            empty_multi_head_loop_state(self.batch_size, self.n_heads_enc)
#            for a in range(self.depth)]

        histories["decoded_symbols"] = tf.zeros(
            shape=[0, self.batch_size],
            dtype=tf.int32,
            name="decoded_symbols")

        histories["input_mask"] = tf.zeros(
            shape=[0, self.batch_size],
            dtype=tf.float32,
            name="input_mask")

        # TransformerHistories is a type and should be callable
        # pylint: disable=not-callable
        tr_histories = TransformerHistories(**histories)
        # pylint: enable=not-callable

        return LoopState(
            histories=tr_histories,
            constants=[],
            feedables=default_ls.feedables)

    def get_body(self, train_mode: bool, sample: bool = False) -> Callable:
        assert not train_mode

        # pylint: disable=too-many-locals
        def body(*args) -> LoopState:

            loop_state = LoopState(*args)
            histories = loop_state.histories
            feedables = loop_state.feedables
            step = feedables.step

            # shape (time, batch)
            decoded_symbols = tf.concat(
                [histories.decoded_symbols, tf.expand_dims(
                    feedables.input_symbol, 0)],
                axis=0)

            input_mask = tf.concat(
                [histories.input_mask, tf.expand_dims(
                    tf.to_float(tf.logical_not(feedables.finished)), 0)],
                axis=0)

            # shape (batch, time)
            decoded_symbols_in_batch = tf.transpose(decoded_symbols)

            # mask (time, batch)
            mask = input_mask

            with tf.variable_scope(self._variable_scope, reuse=tf.AUTO_REUSE):
                # shape (batch, time, dimension)
                embedded_inputs = self.embed_inputs(decoded_symbols_in_batch)

                last_layer = self.layer(
                    self.depth, embedded_inputs, tf.transpose(mask))

                # (batch, state_size)
                output_state = last_layer.temporal_states[:, -1, :]

                # See train_logits definition
                logits = tf.matmul(output_state, self.decoding_w)
                logits += self.decoding_b

                if sample:
                    next_symbols = tf.multinomial(logits, num_samples=1)
                else:
                    next_symbols = tf.to_int32(tf.argmax(logits, axis=1))
                    int_unfinished_mask = tf.to_int32(
                        tf.logical_not(loop_state.feedables.finished))

                    # Note this works only when PAD_TOKEN_INDEX is 0. Otherwise
                    # this have to be rewritten
                    assert PAD_TOKEN_INDEX == 0
                    next_symbols = next_symbols * int_unfinished_mask

                    has_just_finished = tf.equal(next_symbols, END_TOKEN_INDEX)
                    has_finished = tf.logical_or(feedables.finished,
                                                 has_just_finished)
                    not_finished = tf.logical_not(has_finished)

            new_feedables = DecoderFeedables(
                step=step + 1,
                finished=has_finished,
                input_symbol=next_symbols,
                prev_logits=logits)

            # TransformerHistories is a type and should be callable
            # pylint: disable=not-callable
            new_histories = TransformerHistories(
                logits=tf.concat(
                    [histories.logits, tf.expand_dims(logits, 0)], 0),
                decoder_outputs=tf.concat(
                    [histories.decoder_outputs,
                     tf.expand_dims(output_state, 0)],
                    axis=0),
                mask=tf.concat(
                    [histories.mask, tf.expand_dims(not_finished, 0)], 0),
                outputs=tf.concat(
                    [histories.outputs,
                     tf.expand_dims(next_symbols, 0)],
                    axis=0),
                # transformer-specific:
                # TODO handle attention histories correctly
                decoded_symbols=decoded_symbols,
                #self_attention_histories=histories.self_attention_histories,
                #inter_attention_histories=histories.inter_attention_histories,
                input_mask=input_mask)
            # pylint: enable=not-callable

            new_loop_state = LoopState(
                histories=new_histories,
                constants=[],
                feedables=new_feedables)

            return new_loop_state
        # pylint: enable=too-many-locals

        return body
