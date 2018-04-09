import tensorflow as tf
from typeguard import check_argument_types

from neuralmonkey.dataset import Dataset
from neuralmonkey.decorators import tensor
from neuralmonkey.model.model_part import ModelPart, FeedDict, InitializerSpecs
from neuralmonkey.model.stateful import (
    SpatialStateful, SpatialStatefulWithOutput)
from neuralmonkey.tf_utils import get_variable


class SpatialEmbeddingsAdder(ModelPart, SpatialStatefulWithOutput):

    def __init__(
            self,
            name: str,
            input_map: SpatialStateful,
            position_embedding_size: int = None,
            horizontal_embedding_size: int = None,
            vertical_embedding_size: int = None,
            additional_projection: int = None,
            save_checkpoint: str = None,
            load_checkpoint: str = None,
            initializers: InitializerSpecs = None) -> None:
        check_argument_types()
        ModelPart.__init__(self, name, save_checkpoint, load_checkpoint,
                           initializers)

        if (position_embedding_size is None and
                horizontal_embedding_size is None
                and vertical_embedding_size is None):
            raise ValueEror(
                "At least one type of position embeedings must be specified.")

        self.input_map = input_map
        self.position_embedding_size = position_embedding_size
        self.horizontal_embedding_size = horizontal_embedding_size
        self.vertical_embedding_size = vertical_embedding_size
        self.additional_projection = additional_projection

    @tensor
    def spatial_mask(self) -> tf.Tensor:
        return self.input_map.spatial_mask

    @tensor
    def spatial_embeddings(self) -> tf.Tensor:
        shape = self.input_map.spatial_states.get_shape().as_list()[:3]
        assert len(shape) == 3
        shape[0] = 1

        embeddings = []

        if self.vertical_embedding_size is not None:
            vertical_embedding = get_variable(
                name="vertical_embeddings",
                shape=[1, shape[1], 1, self.vertical_embedding_size])
            tiled_to_width = tf.tile(vertical_embedding, [1, 1, shape[2], 1])
            embeddings.append(tiled_to_width)

        if self.horizontal_embedding_size is not None:
            horizontal_embedding = get_variable(
                name="horizontal_embedding",
                shape=[1, 1, shape[2], self.horizontal_embedding_size])
            tiled_to_height = tf.tile(horizontal_embedding, [1, shape[1], 1, 1])
            embeddings.append(tiled_to_height)

        if self.position_embedding_size is not None:
            embeddings.append(get_variable(
                "spatial_embeddings", shape=shape + [self.position_embedding_size]))

        return tf.concat(embeddings, axis=3)

    @tensor
    def spatial_states(self) -> tf.Tensor:
        batch_size = tf.shape(self.input_map.spatial_states)[0]
        tiled_embeddings = tf.tile(
            self.spatial_embeddings, [batch_size, 1, 1, 1])

        concat = tf.concat(
            [tiled_embeddings, self.input_map.spatial_states], axis=3)

        if self.additional_projection is not None:
            return tf.layers.conv2d(
                concat, filters=self.additional_projection, kernel_size=1)
        return concat

    @tensor
    def output(self) -> tf.Tensor:
        return tf.reduce_mean(
            self.spatial_states, axis=[1, 2], name="average_state")

    def feed_dict(self, dataset: Dataset, train: bool = False) -> FeedDict:
        return {}

    def get_dependencies(self):
        return set([self]).union(self.input_map.get_dependencies())
