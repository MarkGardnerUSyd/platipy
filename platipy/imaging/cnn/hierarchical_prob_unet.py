# Copyright 2020 University of New South Wales, University of Sydney, Ingham Institute

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# This code is adapted from
# https://github.com/deepmind/deepmind-research/tree/5cf55efe1f1748ebdd33cb69223b0df6bcc88e6a/hierarchical_probabilistic_unet
# which is released under the Apache Licence 2.0

# pylint: disable=invalid-name

import torch


class ResBlock(torch.nn.Module):
    """A residual block"""

    def __init__(
        self,
        input_channels,
        output_channels,
        n_down_channels=None,
        activation_fn=torch.nn.ReLU,
        convs_per_block=3,
    ):
        """Create a residual block

        Args:
            input_channels (int): The number of input channels to the block
            output_channels (int): The number of output channels from the block
            n_down_channels (int, optional): The number of intermediate cahnnels within the block.
                                             Defaults to the same as the number of output channels.
            activation_fn (torch.nn.Module, optional): The activation function to apply. Defaults
                                                       to torch.nn.ReLU.
            convs_per_block (int, optional): The number of convolutions to perform within the
                                             block. Defaults to 3.
        """

        super(ResBlock, self).__init__()

        self._activation_fn = activation_fn()

        # Set the number of intermediate channels that we compress to.
        if n_down_channels is None:
            n_down_channels = output_channels

        layers = []
        in_channels = input_channels
        for c in range(convs_per_block):
            layers.append(
                torch.nn.Conv2d(
                    in_channels=in_channels, out_channels=n_down_channels, kernel_size=3, padding=1
                )
            )

            if c < convs_per_block - 1:
                layers.append(activation_fn())

            in_channels = n_down_channels

        if not n_down_channels == output_channels:
            resize_outgoing = torch.nn.Conv2d(
                in_channels=n_down_channels, out_channels=output_channels, kernel_size=1, padding=0
            )
            layers.append(resize_outgoing)

        self._layers = torch.nn.Sequential(*layers)

        self._resize_skip = None

        if not input_channels == output_channels:
            self._resize_skip = torch.nn.Conv2d(
                in_channels=input_channels, out_channels=output_channels, kernel_size=1, padding=0
            )

    def forward(self, input_features):

        # Pre-activate the inputs.
        skip = input_features
        residual = self._activation_fn(input_features)

        for layer in self._layers:
            residual = layer(residual)

        if not self._resize_skip is None:
            skip = self._resize_skip(skip)

        return skip + residual


def resize_up(input_features, scale=2):
    """Resize the the input to upsample

    Args:
        input_features (torch.Tensor): The Tensor to upsize
        scale (int, optional): The scale used to upsize. Defaults to 2.

    Returns:
        torch.Tensor: The upsized Tensor
    """
    _, _, size_x, size_y = input_features.shape
    new_size_x = int(round(size_x * scale))
    new_size_y = int(round(size_y * scale))
    return torch.nn.functional.interpolate(input_features, size=[new_size_x, new_size_y])


def resize_down(input_features, scale=2):
    """Resize the the input to downsample

    Args:
        input_features (torch.Tensor): The Tensor to downsize
        scale (int, optional): The scale used to downsize. Defaults to 2.

    Returns:
        torch.Tensor: The downsized Tensor
    """
    return torch.nn.AvgPool2d(kernel_size=scale, stride=scale, padding=0)(input_features)


class _HierarchicalCore(torch.nn.Module):
    """A U-Net encoder-decoder with a full encoder and a truncated decoder.
    The truncated decoder is interleaved with the hierarchical latent space and
    has as many levels as there are levels in the hierarchy plus one additional
    level.
    """

    def __init__(
        self,
        latent_dims,
        input_channels,
        channels_per_block,
        down_channels_per_block=None,
        activation_fn=torch.nn.ReLU,
        convs_per_block=3,
        blocks_per_level=3,
    ):
        """Initializes a HierarchicalCore.

        Args:
            latent_dims (list): List of integers specifying the dimensions of the latents at
                                each scale. The length of the list indicates the number of U-Net
                                decoder scales that have latents.
            input_channels (int): The number of input channels.
            channels_per_block (list): A list of integers specifying the number of output
                                         channels for each encoder block.
            down_channels_per_block (list, optional): A list of integers specifying the number of
                                                      intermediate channels for each encoder block
                                                      or None. If None, the intermediate channels
                                                      are chosen equal to channels_per_block.
                                                      Defaults to None.
            activation_fn (torch.nn.Module, optional): A callable activation function. Defaults to
                                                       torch.nn.ReLU.
            convs_per_block (int, optional): An integer specifying the number of convolutional
                                             layers. Defaults to 3.
            blocks_per_level (int, optional): An integer specifying the number of residual blocks
                                              per level. Defaults to 3.
        """

        super(_HierarchicalCore, self).__init__()

        self._latent_dims = latent_dims
        self._input_channels = input_channels
        self._channels_per_block = channels_per_block
        self._activation_fn = activation_fn
        self._convs_per_block = convs_per_block
        self._blocks_per_level = blocks_per_level
        if down_channels_per_block is None:
            self._down_channels_per_block = channels_per_block
        else:
            self._down_channels_per_block = down_channels_per_block

        num_levels = len(self._channels_per_block)
        self._num_latent_levels = len(self._latent_dims)

        # Iterate the descending levels in the U-Net encoder.
        self.encoder_layers = torch.nn.ModuleList()
        in_channels = input_channels
        for level in range(num_levels):
            # Iterate the residual blocks in each level.
            layer = []
            for _ in range(self._blocks_per_level):
                layer.append(
                    ResBlock(
                        in_channels,
                        channels_per_block[level],
                        n_down_channels=self._down_channels_per_block[level],
                        activation_fn=self._activation_fn,
                        convs_per_block=self._convs_per_block,
                    )
                )
                in_channels = channels_per_block[level]

            self.encoder_layers.append(torch.nn.Sequential(*layer))

        # Iterate the ascending levels in the (truncated) U-Net decoder.
        self.decoder_layers = torch.nn.ModuleList()
        self._mu_logsigma_blocks = torch.nn.ModuleList()

        for level in range(self._num_latent_levels):

            latent_dim = latent_dims[level]

            mu_logsigma_block = torch.nn.Conv2d(
                channels_per_block[::-1][level], 2 * latent_dim, kernel_size=1, padding=0
            )

            self._mu_logsigma_blocks.append(mu_logsigma_block)

            decoder_in_channels = (
                channels_per_block[::-1][level + 1] + channels_per_block[::-1][level]
            ) + latent_dim
            layer = []
            for _ in range(self._blocks_per_level):
                layer.append(
                    ResBlock(
                        decoder_in_channels,
                        channels_per_block[::-1][level + 1],
                        n_down_channels=self._down_channels_per_block[::-1][level + 1],
                        activation_fn=self._activation_fn,
                        convs_per_block=self._convs_per_block,
                    )
                )
                decoder_in_channels = channels_per_block[::-1][level + 1]

            self.decoder_layers.append(torch.nn.Sequential(*layer))

    def forward(self, inputs, mean=False, z_q=None):
        """Forward pass to sample from the module as specified.

        Args:
            inputs (torch.Tensor): A tensor of shape (b,c,h,w). When using the module as a prior
                                   the `inputs` tensor should be a batch of images. When using it
                                   as a posterior the tensor should be a (batched) concatentation
                                   of images and segmentations.
            mean (bool|list, optional): A boolean or a list of booleans. If a boolean, it specifies
                                        whether or not to use the distributions' means in ALL
                                        latent scales. If a list, each bool therein specifies
                                        whether or not to use the scale's mean. If False, the
                                        latents of the scale are sampled. Defaults to False.
            z_q (list, optional): None or a list of tensors. If not None, z_q provides external
                                  latents to be used instead of sampling them. This is used to
                                  employ posterior latents in the prior during training. Therefore,
                                  if z_q is not None, the value of `mean` is ignored. If z_q is
                                  None, either the distributions mean is used (in case `mean` for
                                  the respective scale is True) or else a sample from the
                                  distribution is drawn. Defaults to None.

        Returns:
            dict: A Dictionary holding the output feature map of the truncated U-Net decoder under
            key 'decoder_features', a list of the U-Net encoder features produced at the end of
            each encoder scale under key 'encoder_outputs', a list of the predicted distributions
            at each scale under key 'distributions', a list of the used latents at each scale under
            the key 'used_latents'.
        """

        encoder_features = inputs
        encoder_outputs = []
        num_levels = len(self._channels_per_block)
        num_latent_levels = len(self._latent_dims)
        if isinstance(mean, bool):
            mean = [mean] * self._num_latent_levels
        distributions = []
        used_latents = []

        # Iterate the descending levels in the U-Net encoder.
        for level, encoder_layer in enumerate(self.encoder_layers):
            encoder_features = encoder_layer(encoder_features)
            encoder_outputs.append(encoder_features)
            if not level == num_levels - 1:
                encoder_features = resize_down(encoder_features, scale=2)

        # Iterate the ascending levels in the (truncated) U-Net decoder.
        decoder_features = encoder_outputs[-1]
        for level in range(num_latent_levels):

            # Predict a Gaussian distribution for each pixel in the feature map.
            latent_dim = self._latent_dims[level]
            mu_logsigma = self._mu_logsigma_blocks[level](decoder_features)

            mu = mu_logsigma[:, :latent_dim]
            log_sigma = mu_logsigma[:, latent_dim:]

            dist = torch.distributions.Independent(
                torch.distributions.Normal(loc=mu, scale=torch.exp(log_sigma)), 1
            )
            distributions.append(dist)

            # Get the latents to condition on.
            if z_q is not None:
                z = z_q[level]
            elif mean[level]:
                z = dist.base_dist.loc
            else:
                z = dist.sample()

            used_latents.append(z)

            # Concat and upsample the latents with the previous features.
            decoder_output_lo = torch.cat([z, decoder_features], axis=1)
            decoder_output_hi = resize_up(decoder_output_lo, scale=2)
            decoder_features = torch.cat(
                [decoder_output_hi, encoder_outputs[::-1][level + 1]], axis=1
            )
            decoder_features = self.decoder_layers[level](decoder_features)

        return {
            "decoder_features": decoder_features,
            "encoder_features": encoder_outputs,
            "distributions": distributions,
            "used_latents": used_latents,
        }


class _StitchingDecoder(torch.nn.Module):
    """A module that completes the truncated U-Net decoder.
    Using the output of the HierarchicalCore this module fills in the missing
    decoder levels such that together the two form a symmetric U-Net.
    """

    def __init__(
        self,
        latent_dims,
        channels_per_block,
        num_classes,
        down_channels_per_block=None,
        activation_fn=torch.nn.ReLU,
        convs_per_block=3,
        blocks_per_level=3,
    ):
        """Initializes a StichtingDecoder.

        Args:
            latent_dims (list): List of integers specifying the dimensions of the latents at each
                                scale. The length of the list indicates the number of U-Net decoder
                                scales that have latents.
            channels_per_block (list): A list of integers specifying the number of output channels
                                       for each encoder block.
            num_classes (int): The number of segmentation classes.
            down_channels_per_block ([type], optional): A list of integers specifying the number of
                                                        intermediate channels for each encoder
                                                        block. If None, the intermediate channels
                                                        are chosen equal to channels_per_block.
                                                        Defaults to None.
            activation_fn (torch.nn.Module, optional): A callable activation function.Defaults to
                                                       torch.nn.ReLU.
            initializers ([type], optional): [description]. Defaults to None.
            regularizers ([type], optional): [description]. Defaults to None.
            convs_per_block (int, optional): An integer specifying the number of convolutional
                                             layers. Defaults to 3.
            blocks_per_level (int, optional): An integer specifying the number of residual blocks
                                              per level. Defaults to 3.
        """
        super(_StitchingDecoder, self).__init__()
        self._latent_dims = latent_dims
        self._channels_per_block = channels_per_block
        self._num_classes = num_classes
        self._activation_fn = activation_fn
        self._convs_per_block = convs_per_block
        self._blocks_per_level = blocks_per_level
        if down_channels_per_block is None:
            down_channels_per_block = channels_per_block
        self._down_channels_per_block = down_channels_per_block

        num_latents = len(self._latent_dims)
        self._start_level = num_latents + 1
        self._num_levels = len(self._channels_per_block)

        self.decoder_layers = torch.nn.ModuleList()
        for level in range(self._start_level, self._num_levels, 1):

            decoder_in_channels = (
                channels_per_block[::-1][level - 1] + channels_per_block[::-1][level]
            )
            layer = []
            for _ in range(self._blocks_per_level):
                layer.append(
                    ResBlock(
                        decoder_in_channels,
                        channels_per_block[::-1][level],
                        n_down_channels=self._down_channels_per_block[::-1][level],
                        activation_fn=self._activation_fn,
                        convs_per_block=self._convs_per_block,
                    )
                )
                decoder_in_channels = channels_per_block[::-1][level]

            self.decoder_layers.append(torch.nn.Sequential(*layer))

            self.final_layer = torch.nn.Conv2d(
                decoder_in_channels, self._num_classes, kernel_size=1, padding=0
            )

    def forward(self, encoder_features, decoder_features):
        """Forward pass through the stiching decoder

        Args:
            encoder_features (torch.Tensor): Tensor of encoder features
            decoder_features (dict): Tensor of decoder features

        Returns:
            torch.Tensor: The stiched output
        """

        for level in range(len(self.decoder_layers)):
            enc_level = self._start_level + level
            decoder_features = resize_up(decoder_features, scale=2)
            decoder_features = torch.cat(
                [decoder_features, encoder_features[::-1][enc_level]], axis=1
            )
            decoder_features = self.decoder_layers[level](decoder_features)

        return self.final_layer(decoder_features)


class HierarchicalProbabilisticUnet(torch.nn.Module):
    """A hierarchical probabilistic UNet implementation: https://arxiv.org/abs/1905.13077"""

    def __init__(
        self,
        input_channels=1,
        num_classes=2,
        channels_per_block=None,
        down_channels_per_block=None,
        latent_dims=(1, 1, 1, 1),
        convs_per_block=3,
        blocks_per_level=3,
        loss_kwargs=None,
    ):
        """Initialize the Hierarchical Probabilistic UNet

        Args:
            input_channels (int, optional): The number of channels in the image (1 for
                                            greyscale and 3 for RGB). Defaults to 1.
            num_classes (int, optional): The number of classes to predict. Defaults to 2.
            channels_per_block (list, optional): A list of channels to use in blocks of each
                                                 layer the amount of filters layer. Defaults
                                                 to None.
            down_channels_per_block (list, optional): [description]. Defaults to None.
            latent_dims (tuple, optional): The number of latent dimensions at each layer.
                                           Defaults to (1, 1, 1, 1).
            convs_per_block (int, optional): An integer specifying the number of convolutional
                                             layers. Defaults to 3. Defaults to 3.
            blocks_per_level (int, optional): An integer specifying the number of residual
                                              blocks per level. Defaults to 3.
            loss_kwargs (dict, optional): Dictionary of argument used by loss function.
                                          Defaults to None.
        """
        super(HierarchicalProbabilisticUnet, self).__init__()

        base_channels = 24
        default_channels_per_block = (
            base_channels,
            2 * base_channels,
            4 * base_channels,
            8 * base_channels,
            8 * base_channels,
            8 * base_channels,
            8 * base_channels,
            8 * base_channels,
        )
        if channels_per_block is None:
            channels_per_block = default_channels_per_block
        if down_channels_per_block is None:
            down_channels_per_block = [int(i / 2) for i in default_channels_per_block]

        self._prior = _HierarchicalCore(
            input_channels=input_channels,
            latent_dims=latent_dims,
            channels_per_block=channels_per_block,
            down_channels_per_block=down_channels_per_block,
            convs_per_block=convs_per_block,
            blocks_per_level=blocks_per_level,
        )

        self._posterior = _HierarchicalCore(
            input_channels=input_channels + num_classes,
            latent_dims=latent_dims,
            channels_per_block=channels_per_block,
            down_channels_per_block=down_channels_per_block,
            convs_per_block=convs_per_block,
            blocks_per_level=blocks_per_level,
        )

        self._f_comb = _StitchingDecoder(
            latent_dims=latent_dims,
            channels_per_block=channels_per_block,
            num_classes=num_classes,
            down_channels_per_block=down_channels_per_block,
            convs_per_block=convs_per_block,
            blocks_per_level=blocks_per_level,
        )

        self._cache = None

        if loss_kwargs is None:
            self._loss_kwargs = {
                "type": "elbo",
                "kappa": 0.05,
                "decay": 0.99,
                "rate": 1e-2,
                "beta": 1.0,
            }
        else:
            self._loss_kwargs = loss_kwargs

        # if self._loss_kwargs["type"] == "geco":
        #     self._moving_average = ExponentialMovingAverage(
        #         model=self, decay=self._loss_kwargs["decay"]
        #     )
        #     self._lagmul = geco_utils.LagrangeMultiplier(rate=self._loss_kwargs["rate"])

        self._q_sample = None
        self._q_sample_mean = None
        self._p_sample = None
        self._p_sample_z_q = None
        self._p_sample_z_q_mean = None

    def forward(self, img, seg):
        """Inserts all ops used during training into the graph exactly once. The first time this
        method is called given the input pair (img, seg) all ops relevant for training are inserted
        into the graph. Calling this method more than once does not re-insert the modules into the
        graph (memoization), thus preventing multiple forward passes of submodules for the same
        inputs.

        Args:
            img (torch.Tensor): A tensor of shape (b, c, h, w).
            seg (torch.Tensor): A tensor of shape (b, num_classes, h, w).
        """

        input_tensor = torch.cat([img, seg], axis=1)

        if not self._cache is None and torch.equal(self._cache, input_tensor):
            # No need to recompute
            return

        self._q_sample = self._posterior(input_tensor, mean=False)
        self._q_sample_mean = self._posterior(input_tensor, mean=True)
        self._p_sample = self._prior(img, mean=False, z_q=None)
        self._p_sample_z_q = self._prior(img, z_q=self._q_sample["used_latents"])
        self._p_sample_z_q_mean = self._prior(img, z_q=self._q_sample_mean["used_latents"])
        self._cache = input_tensor

    def sample(self, img, mean=False, z_q=None):
        """Sample a segmentation from the prior, given an input image.

        Args:
            img (torch.Tensor): A tensor of shape (b, c, h, w).
            mean (bool, optional): A boolean or a list of booleans. If a boolean, it specifies
                                   whether or not to use the distributions' means in ALL latent
                                   scales. If a list, each bool therein specifies whether or not to
                                   use the scale's mean. If False, the latents of the scale are
                                   sampled. Defaults to False.
            z_q (list, optional): If not None, z_q provides external latents to be used instead of
                                  sampling them. This is used to employ posterior latents in the
                                  prior during training. Therefore, if z_q is not None, the value
                                  of `mean` is ignored. If z_q is None, either the distributions
                                  mean is used (in case `mean` for the respective scale is True) or
                                  else a sample from the distribution is drawn. Defaults to None.

        Returns:
            torch.Tensor: A segmentation tensor of shape (b, num_classes, h, w).
        """

        prior_out = self._prior(img, mean, z_q)
        encoder_features = prior_out["encoder_features"]
        decoder_features = prior_out["decoder_features"]
        return self._f_comb(encoder_features, decoder_features)

    def reconstruct(self, img, seg, mean=False):
        """Reconstruct a segmentation using the posterior.

        Args:
            img ([torch.Tensor): A tensor of shape (b, c, h, w).
            seg (torch.Tensor): A tensor of shape (b, num_classes, h, w).
            mean (bool, optional): A boolean, specifying whether to sample from the full hierarchy
                                   of the posterior or use the posterior means at each scale of the
                                   hierarchy. Defaults to False.

        Returns:
            torch.Tensor: A segmentation tensor of shape (b,num_classes,h,w).
        """

        self.forward(img, seg)
        if mean:
            prior_out = self._p_sample_z_q_mean
        else:
            prior_out = self._p_sample_z_q
        encoder_features = prior_out["encoder_features"]
        decoder_features = prior_out["decoder_features"]
        return self._f_comb(encoder_features, decoder_features)

    def kl(self, img, seg):
        """Kullback-Leibler divergence between the posterior and the prior.

        Args:
            img (torch.Tensor): A tensor of shape (b, c, h, w).
            seg (torch.Tensor): A tensor of shape (b, num_classes, h, w).

        Returns:
            dict: A dictionary with keys indexing the hierarchy's levels and corresponding
                    values holding the KL-term for each level (per batch).
        """
        self.forward(img, seg)
        posterior_out = self._q_sample
        prior_out = self._p_sample_z_q

        q_dists = posterior_out["distributions"]
        p_dists = prior_out["distributions"]

        kl = {}
        for level, (p, q) in enumerate(zip(p_dists, q_dists)):
            kl_per_pixel = torch.distributions.kl.kl_divergence(p, q)
            kl_per_instance = torch.sum(kl_per_pixel, [1, 2])
            kl[level] = torch.mean(kl_per_instance)

        return kl

    def rec_loss(self, img, seg):
        """Cross-entropy reconstruction loss employed in the ELBO-/ GECO-objective.

        Args:
            img (torch.Tensor): A tensor of shape (b, c, h, w).
            seg (torch.Tensor): A tensor of shape (b, num_classes, h, w).

        Returns:
            dict: A dictionary holding the mean and the pixelwise sum of the loss
        """
        reconstruction = self.reconstruct(img, seg, mean=False)

        criterion = torch.nn.BCEWithLogitsLoss(reduction="none")
        reconstruction_loss = criterion(input=reconstruction, target=seg)
        reconstruction_loss_sum = torch.sum(reconstruction_loss)
        reconstruction_loss_mean = torch.mean(reconstruction_loss)

        return {"mean": reconstruction_loss_mean, "sum": reconstruction_loss_sum}

    def loss(self, img, seg):
        """The full training objective, either ELBO or GECO.

        Args:
            img (torch.Tensor): A tensor of shape (b, c, h, w).
            seg (torch.Tensor): A tensor of shape (b, num_classes, h, w).

        Raises:
            NotImplementedError: Raised if loss function supplied isn't implemented yet.

        Returns:
            dict: A dictionary holding the loss (with key 'loss')
        """
        summaries = {}
        rec_loss = self.rec_loss(img, seg)

        kl_dict = self.kl(img, seg)
        kl_sum = torch.sum(torch.stack([kl for _, kl in kl_dict.items()], axis=-1))

        summaries["rec_loss_mean"] = rec_loss["mean"]
        summaries["rec_loss_sum"] = rec_loss["sum"]
        summaries["kl_sum"] = kl_sum
        for level, kl in kl_dict.items():
            summaries["kl_{}".format(level)] = kl

        # Set up a regular ELBO objective.
        if self._loss_kwargs["type"] == "elbo":
            loss = rec_loss["sum"] + self._loss_kwargs["beta"] * kl_sum
            summaries["elbo_loss"] = loss

        # TODO Still need to implement geco
        # Set up a GECO objective (ELBO with a reconstruction constraint).
        # elif self._loss_kwargs["type"] == "geco":
        #     ma_rec_loss = self._moving_average(rec_loss["sum"])
        #     mask_sum_per_instance = torch.sum(rec_loss["mask"], -1)
        #     num_valid_pixels = torch.mean(mask_sum_per_instance)
        #     reconstruction_threshold = self._loss_kwargs["kappa"] * num_valid_pixels

        #     rec_constraint = ma_rec_loss - reconstruction_threshold
        #     lagmul = self._lagmul(rec_constraint)
        #     loss = lagmul * rec_constraint + kl_sum

        #     summaries["geco_loss"] = loss
        #     summaries["ma_rec_loss_mean"] = ma_rec_loss / num_valid_pixels
        #     summaries["num_valid_pixels"] = num_valid_pixels
        #     summaries["lagmul"] = lagmul
        else:
            raise NotImplementedError(
                "Loss type {} not implemeted!".format(self._loss_kwargs["type"])
            )

        return dict(supervised_loss=loss, summaries=summaries)