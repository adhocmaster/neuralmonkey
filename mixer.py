import tensorflow as tf
import numpy as np
from nltk.translate.bleu_score import sentence_bleu, SmoothingFunction
from learning_utils import log

class Mixer(object):

    def __init__(self, decoder):
        self.decoder = decoder

        with tf.variable_scope('mixer'):
            self.bleu = tf.placeholder(tf.float32, [None])

            hidden_states = decoder.hidden_states

            with tf.variable_scope('exprected_reward_regressor'):
                linear_reg_W = tf.Variable(tf.truncated_normal([decoder.rnn_size, 1]))
                linear_reg_b = tf.Variable(tf.zeros([1]))

                expected_rewards = [tf.squeeze(tf.matmul(h, linear_reg_W)) + linear_reg_b for h in hidden_states]

                regression_loss = sum([(r - self.bleu) ** 2 for r in expected_rewards]) * 0.5
                regression_optimizer = tf.train.GradientDescentOptimizer(1e-3).minimize(regression_loss)

            ## decoded logits: [batch * slovnik] (delky max sequence) - obsahuje logity
            ## decoded_seq: [batch * 1] (delky max sequence) - obsahuje indexy do slovniku (argmaxy)

            with tf.variable_scope("reinforce_gradients"):
                max_logits = [ tf.expand_dims(tf.reduce_max(l, 1), 1) for l in decoder.decoded_logits ] ## batch x 1 x 1
                # nasledujici radka pada za runtimu na spatny shape
                indicator = [tf.to_float(tf.equal(ml, l)) for ml, l in zip(max_logits, decoder.decoded_logits)] ## batch x slovnik

                log("Forward graph ready")

                derivatives = [ tf.reduce_sum(tf.expand_dims(self.bleu - r, 1) *  (tf.nn.softmax(l) - i), 0, keep_dims=True) \
                                    for r, l, i in zip(expected_rewards, decoder.decoded_logits, indicator)] ## [1xslovnik](delky max sequence)
                derivatives_stopped = [tf.stop_gradient(d) for d in derivatives]

                trainable_vars = [v for v in tf.trainable_variables() if not v.name.startswith('mixer')]

                reinforce_gradients = [tf.gradients(l * d, trainable_vars)  for l, d in zip(decoder.decoded_logits, derivatives_stopped)]
                ## [slovnik x shape promenny](delky max seq)

                log("Reinfoce gradients computed")

            with tf.variable_scope("cross_entropy_gradients"):
                cross_entropies = [tf.reduce_sum(tf.nn.sparse_softmax_cross_entropy_with_logits(l, t) * w, 0) \
                                       for l, t, w in zip(decoder.decoded_logits, decoder.targets, decoder.weights_ins)] ## [skalar](v case)

                xent_gradients = [tf.gradients(e, trainable_vars) for e in cross_entropies]
                log("Cross-entropy gradients computed")
            self.mixer_weights = [tf.placeholder(tf.float32, []) for _ in hidden_states]

            mixed_gradients = [] # a list for each of the traininable variables

            for i, (rgs, xent_gs, mix_w) in enumerate(zip(reinforce_gradients, xent_gradients, self.mixer_weights)):
                for j, (rg, xent_g) in enumerate(zip(rgs, xent_gs)):
                    if xent_g is None and i == 0:
                        mixed_gradients.append(None)
                        continue

                    if type(xent_g) == tf.Tensor or type(xent_g) == tf.IndexedSlices:
                        g = tf.add(tf.scalar_mul(mix_w, xent_g), tf.scalar_mul(1 - mix_w, rg))
                    elif xent_g is None:
                        continue
                    else:
                        raise Exception("Unnkown type of gradients: {}".format(type(xg)))

                    if i == 0:
                        mixed_gradients.append(g)
                    else:
                        if mixed_gradients[j] is None:
                            mixed_gradients[j] = g
                        else:
                            mixed_gradients[j] += g

            self.mixer_optimizer = \
                    tf.train.AdamOptimizer().apply_gradients(zip(mixed_gradients, trainable_vars))


    def run(self, sess, fd, references, verbose=False):
        # TODO for some first steps call the XENT traininer onl

        # TODO compute gradually add renforce steps

        decoded_sequence = sess.run(self.decoder.decoded_seq, feed_dict=fd)
        sentences = self.decoder.vocabulary.vectors_to_sentences(decoded_sequence)
        bleu_smoothing = SmoothingFunction(epsilon=0.01).method1
        bleus = [sentence_bleu(r, s, smoothing_function=bleu_smoothing) for r, s in zip(references, sentences)]

        fd[self.bleu] = bleus

        for w in self.mixer_weights:
            fd[w] = 1

        if verbose:
            return sess.run([self.mixer_optimizer, self.decoder.loss_with_decoded_ins,
                             self.decoder.loss_with_gt_ins, self.decoder.summary_train] + self.decoder.decoded_seq,
                            feed_dict=fd)
        else:
            return sess.run([self.mixer_optimizer], feed_dict=fd)
