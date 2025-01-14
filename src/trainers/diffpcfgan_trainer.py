import logging
import math
import typing

import matplotlib.pyplot as plt
import numpy as np
import seaborn as sns
import torch
import torch.nn as nn

from src.metrics.epdf import HistogramLoss
from src.trainers.trainer import Trainer
from src.utils.utils_os import savefig

logger = logging.getLogger(__name__)


from src.PCF_with_empirical_measure import PCF_with_empirical_measure
from src.differentialequations.diffusionprocess_continuous import (
    SDEType,
    ContinuousDiffusionProcess,
)


# TODO 12/08/2024 nie_k: Add a way to add a zero at the beginning of a sequence without having to sample it for Swissroll.
# TODO 12/08/2024 nie_k: Alternative plot for swiss roll.

PERIOD_PLOT_VAL = 100
sns.set()

PLOT_DIFFUSION_FIG, PLOT_DIFFUSION_AXES = plt.subplots(1, 2, sharey=True)
NUM_STEPS_DIFFUSION_2_CONSIDER = 8
# Adding 1 for the zero at the beginning.
NUM_STEPS_DIFFUSION_2_CONSIDER += 1


class DiffPCFGANTrainer(Trainer):

    @staticmethod
    def get_noise_vector(shape: typing.Tuple[int, ...], device: str) -> torch.Tensor:
        return torch.randn(*shape, device=device)

    @staticmethod
    def _flat_add_time_transpose_and_add_zero(data: torch.Tensor) -> torch.Tensor:
        # Receive data of shape (S, N, L, D).
        # Transforms it into (N, S, L * D)

        # Merge the sequence dimension (dim 2) and the feature dimension (dim 3) into a single dimension.
        # Flattening is required before adding time otherwise we would add too many dimensions with the time.
        data = data.flatten(2, 3)

        # Add a zero at the beginning of the sequence. By adding it before time simplifies time augmentation.
        # WIP MOVE THIS
        zeros = torch.zeros(1, data.shape[1], data.shape[2], device=data.device)
        data = torch.cat((zeros, data), dim=0)

        # Add the times to the data by creating a linspace between 0,1 and which is repeated for all (N,L).
        diffusion_times = (
            torch.linspace(
                0.0,
                1.0,
                steps=data.shape[0],
                device=data.device,
            )
            .view(-1, 1, 1)
            .expand(
                data.shape[0],
                data.shape[1],
                1,
            )
        )
        data = torch.cat((data, diffusion_times), dim=-1)
        # Permute the batch axis and the diffusion axis.
        data = data.transpose(0, 1)
        # adding contiguous slows down the code tremendously, so we keep it like that.
        return data

    def __init__(
        self,
        data_train,
        data_val,
        score_network: nn.Module,
        config,
        learning_rate_gen,
        learning_rate_disc,
        num_D_steps_per_G_step,
        num_samples_pcf,
        hidden_dim_pcf,
        num_diffusion_steps,
        use_fixed_measure_discriminator_pcfd=False,
    ):
        # score_network is used to denoise the data and will be called as score_net(data, time).
        super().__init__(
            test_metrics_train=None,
            test_metrics_test=None,
            feature_dim_time_series=config.input_dim,
        )

        # Parameter for pytorch lightning
        self.automatic_optimization = False

        # Training params
        self.config = config
        self.lr_gen = learning_rate_gen
        self.lr_disc = learning_rate_disc

        # Score Network Params
        self.score_network = score_network

        # Discriminator Params
        self.num_samples_pcf = num_samples_pcf
        self.hidden_dim_pcf = hidden_dim_pcf
        self.discriminator = PCF_with_empirical_measure(
            num_samples=self.num_samples_pcf,
            hidden_size=self.hidden_dim_pcf,
            # TODO 13/08/2024 nie_k: instead of input_dim, set time_series_for_compar_dim
            input_size=self.config.input_dim * self.config.n_lags + 1,
        )
        self.D_steps_per_G_step = num_D_steps_per_G_step
        self.use_fixed_measure_discriminator_pcfd = use_fixed_measure_discriminator_pcfd

        self.output_dir_images = config.exp_dir

        # Diffusion:
        self.diffusion_process = ContinuousDiffusionProcess(
            total_steps=num_diffusion_steps,
            schedule="cosine",
            sde_type=SDEType.VP,
        )
        self.num_diffusion_steps = num_diffusion_steps

        ### Loses
        self.use_diffusion_score_matching_loss = True
        self.reconstruction_loss = torch.nn.MSELoss()
        # Instantiate the HistogramLoss
        self.train_histo_loss = HistogramLoss(
            data_train, int(round(2.0 * math.pow(data_train.shape[0], 1.0 / 3.0), 0))
        )
        self.val_histo_loss = HistogramLoss(
            data_val, int(round(2.0 * math.pow(data_val.shape[0], 1.0 / 3.0), 0))
        )
        return

    def get_backward_path(
        self,
        *,
        num_seq: typing.Optional[int] = None,
        seq_len: typing.Optional[int] = None,
        dim_seq: typing.Optional[int] = None,
        noise_start_seq_z: typing.Optional[torch.Tensor] = None,
        proba_teacher_forcing: float = 0.0,
        # Don't reverse, handled inside. The order should be start with original data and finishes with noise.
        # Shape (S,N,L,D).
        teacher_forcing_inputs=None,
    ) -> torch.Tensor:
        # Alias for forward for clarity
        return self(
            num_seq=num_seq,
            seq_len=seq_len,
            dim_seq=dim_seq,
            noise_start_seq_z=noise_start_seq_z,
            proba_teacher_forcing=proba_teacher_forcing,
            teacher_forcing_inputs=teacher_forcing_inputs,
        )

    def forward(
        self,
        *,
        num_seq: typing.Optional[int] = None,
        seq_len: typing.Optional[int] = None,
        dim_seq: typing.Optional[int] = None,
        noise_start_seq_z: typing.Optional[torch.Tensor] = None,
        proba_teacher_forcing: float = 0.0,
        teacher_forcing_inputs=None,
    ) -> torch.Tensor:
        # Denoise data to generate new samples.
        # Along the first dimension, the first value corresponds to the output data (generated samples).
        assert (
            num_seq is not None and seq_len is not None and dim_seq is not None
        ) or noise_start_seq_z is not None, "Either the three parameters num_seq, seq_len, dim_seq need to be given or the noise_start_seq_z."

        # WIP: explain what noise_start_seq_z we need
        if noise_start_seq_z is None:
            noise_start_seq_z = DiffPCFGANTrainer.get_noise_vector(
                (num_seq, seq_len, dim_seq), self.device
            )

        traj_back = self.diffusion_process.backward_sample(
            noise_start_seq_z,
            self.score_network,
            proba_teacher_forcing=proba_teacher_forcing,
            sequences_forcing=teacher_forcing_inputs,
        )

        # Returns a tensor with shape (num_step_diffusion, num_seq, seq_len, generator.outputdim).
        # Along the first dimension, the first value corresponds to the output data (generated samples).
        return traj_back.flip(0)

    def configure_optimizers(self):
        optim_gen = torch.optim.Adam(
            self.score_network.parameters(),
            lr=self.lr_gen,
            weight_decay=0,
            betas=(0, 0.9),
        )
        optim_discr = torch.optim.Adam(
            self.discriminator.parameters(), lr=self.lr_disc, weight_decay=0
        )
        return [optim_gen, optim_discr], []

    def training_step(self, batch, batch_nb):
        (targets,) = batch
        optim_gen, optim_discr = self.optimizers()

        logger.debug("Targets for training: %s", targets)
        losses_as_dict = self._training_step_gen(optim_gen, targets)

        if not self.use_fixed_measure_discriminator_pcfd:
            for i in range(self.D_steps_per_G_step):
                _ = self._training_step_disc(optim_discr, targets)

        # Discriminator and Generator share the same loss so no need to report both.
        self.log(
            name="train_pcfd",
            value=losses_as_dict["train_pcfd"],
            prog_bar=True,
            on_step=False,
            on_epoch=True,
        )
        self.log(
            name="train_reconst",
            value=losses_as_dict["train_reconst"],
            prog_bar=True,
            on_step=False,
            on_epoch=True,
        )

        self.log(
            name="train_score_matching",
            value=losses_as_dict["train_score_matching"],
            prog_bar=True,
            on_step=False,
            on_epoch=True,
        )

        self.log(
            name="train_epdf",
            value=losses_as_dict["train_epdf"],
            prog_bar=True,
            on_step=False,
            on_epoch=True,
        )

        return

    def validation_step(self, batch, batch_nb):
        (targets,) = batch

        logger.debug("Targets for validation: %s", targets)
        diffused_targets: torch.Tensor = self._get_forward_path(targets, [])
        denoised_diffused_targets: torch.Tensor = self.get_backward_path(
            noise_start_seq_z=diffused_targets[-1],
            proba_teacher_forcing=0.0,
        )

        diffused_targets = DiffPCFGANTrainer._flat_add_time_transpose_and_add_zero(
            diffused_targets
        )
        denoised_diffused_targets = (
            DiffPCFGANTrainer._flat_add_time_transpose_and_add_zero(
                denoised_diffused_targets
            )
        )
        logger.debug(
            "\nDiffused targets for validation: \n%s\nDenoised samples for validation: %s\n",
            diffused_targets,
            denoised_diffused_targets,
        )

        loss_gen = self.discriminator.distance_measure(
            diffused_targets[:, :-1][:, :NUM_STEPS_DIFFUSION_2_CONSIDER],
            denoised_diffused_targets[:, :-1][:, :NUM_STEPS_DIFFUSION_2_CONSIDER],
            lambda_y=0.0,
        )
        self.log(
            name="val_pcfd",
            value=loss_gen,
            prog_bar=True,
            on_step=False,
            on_epoch=True,
        )

        loss_gen_reconst = self.reconstruction_loss(
            diffused_targets[:, 1, :-1], denoised_diffused_targets[:, 1, :-1]
        )
        self.log(
            name="val_reconst",
            value=loss_gen_reconst,
            prog_bar=True,
            on_step=False,
            on_epoch=True,
        )

        loss_gen_score_matching = self._compute_score_matching_loss(targets)
        self.log(
            name="val_score_matching",
            value=loss_gen_score_matching,
            prog_bar=True,
            on_step=False,
            on_epoch=True,
        )

        loss_gen_epdf = self.val_histo_loss(denoised_diffused_targets[:, 1:2, :-1])
        self.log(
            name="val_epdf",
            value=loss_gen_epdf,
            prog_bar=True,
            on_step=False,
            on_epoch=True,
        )

        # TODO 11/08/2024 nie_k: A bit of a hack, I usually code this better but will do the trick for now.
        # TODO 29/08/2024 nie_k: The plot need to be change depending on dataset (manually) and also would not work for sequences
        if not (self.current_epoch + 1) % PERIOD_PLOT_VAL:
            where_image_is_saved = (
                self.output_dir_images
                + f"pred_vs_true_epoch_{str(self.current_epoch + 1)}"
            )
            self.evaluate(
                denoised_diffused_targets[:, 1, :-1],
                targets[:, 0],
                where_image_is_saved,
            )

            self.plot_for_back_ward_trajectories(
                denoised_diffused_targets, diffused_targets
            )
        return

    def plot_for_back_ward_trajectories(
        self, denoised_diffused_targets, diffused_targets
    ):
        denoised_diffused_targets = denoised_diffused_targets.detach().cpu().numpy()
        diffused_targets = diffused_targets.detach().cpu().numpy()

        PLOT_DIFFUSION_AXES[0].clear()
        PLOT_DIFFUSION_AXES[1].clear()

        # Shift by one because we added a trailing zero to the sequences.
        diffusion_steps = np.arange(-1, denoised_diffused_targets.shape[1] - 1)

        for element_dataset in range(diffused_targets.shape[0]):
            PLOT_DIFFUSION_AXES[0].plot(
                diffusion_steps,
                diffused_targets[element_dataset, :, 0],
                linewidth=1.0,
            )
        PLOT_DIFFUSION_AXES[0].set_title("Forward Path")
        PLOT_DIFFUSION_AXES[0].set_xlabel("Diffusion Step")
        for element_dataset in range(denoised_diffused_targets.shape[0]):
            PLOT_DIFFUSION_AXES[1].plot(
                diffusion_steps[::-1],
                denoised_diffused_targets[element_dataset, :, 0],
                linewidth=1.0,
            )
        # Reverse the x-ticks and labels
        # WIP: might lead to too many ticks. See how to handle that.
        PLOT_DIFFUSION_AXES[0].set_xticks(diffusion_steps)
        PLOT_DIFFUSION_AXES[0].set_xticklabels(diffusion_steps)
        PLOT_DIFFUSION_AXES[1].set_xticks(diffusion_steps[::-1])
        PLOT_DIFFUSION_AXES[1].set_xticklabels(diffusion_steps)
        PLOT_DIFFUSION_AXES[1].set_title("Backward Path")
        PLOT_DIFFUSION_AXES[1].set_xlabel("Diffusion Step")
        PLOT_DIFFUSION_FIG.suptitle(
            f"Comparison Diffusion Trajectories for n={diffused_targets.shape[0]}. \nThe distribution are matched over the first {NUM_STEPS_DIFFUSION_2_CONSIDER} steps."
        )
        PLOT_DIFFUSION_FIG.tight_layout()
        savefig(
            PLOT_DIFFUSION_FIG,
            self.output_dir_images + f"trajectories_{str(self.current_epoch + 1)}.png",
        )
        return

    @property
    def proba_teacher_forcing(self):
        return 0.5 * (
            1
            + torch.cos(
                torch.tensor(
                    self.current_epoch * math.pi / (self.trainer.max_epochs // 2)
                )
            )
        )

    def _training_step_gen(
        self, optim_gen, targets: torch.Tensor
    ) -> typing.Dict[str, float]:
        optim_gen.zero_grad()

        diffused_targets: torch.Tensor = self._get_forward_path(targets, [])
        denoised_diffused_targets: torch.Tensor = self.get_backward_path(
            noise_start_seq_z=diffused_targets[-1],
            proba_teacher_forcing=self.proba_teacher_forcing,
            teacher_forcing_inputs=diffused_targets,
        )
        # TODO 06/09/2024 nie_k: If we add a zero, we can remove the last value! Let's see how we handle this.
        diffused_targets = DiffPCFGANTrainer._flat_add_time_transpose_and_add_zero(
            diffused_targets
        )
        denoised_diffused_targets = (
            DiffPCFGANTrainer._flat_add_time_transpose_and_add_zero(
                denoised_diffused_targets
            )
        )
        logger.debug(
            "\nDiffused targets for training: \n%s\nDenoised samples for training: %s\n",
            diffused_targets,
            denoised_diffused_targets,
        )

        loss_gen = self.discriminator.distance_measure(
            diffused_targets[:, :-1][:, :NUM_STEPS_DIFFUSION_2_CONSIDER],
            denoised_diffused_targets[:, :-1][:, :NUM_STEPS_DIFFUSION_2_CONSIDER],
            lambda_y=0.0,
        )
        loss_gen_reconstruction = self.reconstruction_loss(
            diffused_targets[:, 1, :-1], denoised_diffused_targets[:, 1, :-1]
        )

        total_loss = loss_gen + 0.1 * loss_gen_reconstruction

        loss_gen_score_matching = self._compute_score_matching_loss(targets)
        if self.use_diffusion_score_matching_loss:
            total_loss = total_loss + 0.1 * loss_gen_score_matching

        self.manual_backward(total_loss)
        optim_gen.step()

        loss_gen_epdf = self.train_histo_loss(denoised_diffused_targets[:, 1:2, :-1])
        return {
            "train_pcfd": loss_gen,
            "train_reconst": loss_gen_reconstruction,
            "train_score_matching": loss_gen_score_matching,
            "train_epdf": loss_gen_epdf,
        }

    def _training_step_disc(
        self, optim_discr, targets: torch.Tensor
    ) -> typing.Dict[str, float]:
        optim_discr.zero_grad()

        with torch.no_grad():
            diffused_targets: torch.Tensor = self._get_forward_path(targets, [])
            denoised_diffused_targets: torch.Tensor = self.get_backward_path(
                noise_start_seq_z=diffused_targets[-1],
                proba_teacher_forcing=self.proba_teacher_forcing,
                teacher_forcing_inputs=diffused_targets,
            )
            diffused_targets = DiffPCFGANTrainer._flat_add_time_transpose_and_add_zero(
                diffused_targets
            )
            denoised_diffused_targets = (
                DiffPCFGANTrainer._flat_add_time_transpose_and_add_zero(
                    denoised_diffused_targets
                )
            )

        loss_disc = -self.discriminator.distance_measure(
            diffused_targets[:, :-1][:, :NUM_STEPS_DIFFUSION_2_CONSIDER],
            denoised_diffused_targets[:, :-1][:, :NUM_STEPS_DIFFUSION_2_CONSIDER],
            lambda_y=0.0,
        )
        self.manual_backward(loss_disc)
        optim_discr.step()

        return {
            "train_pcfd": loss_disc,
        }

    def _get_forward_path(
        self,
        starting_data: torch.Tensor,
        # Ignore features to be diffused with a hack here.
        indices_features_not_diffuse: typing.Iterable = [-1],
    ) -> torch.Tensor:
        # To get the totally noised data, use: output[-1, :, :, :]

        assert (
            len(starting_data.shape) == 3
        ), f"Incorrect shape for starting_data: Expected 3 dimensions (N, L, D) but got {len(starting_data.shape)} dimensions with shape {starting_data.shape}. Make sure the tensor is correctly reshaped or initialized."

        diffused_starting_data: torch.Tensor = (
            starting_data.clone()
            .unsqueeze(0)
            .repeat(self.num_diffusion_steps + 1, 1, 1, 1)
        )

        mask_where_diffuse = torch.ones(starting_data.shape[-1], dtype=torch.bool)
        mask_where_diffuse[indices_features_not_diffuse] = False

        diffused_starting_data[:, :, :, mask_where_diffuse] = (
            self.diffusion_process.forward_sample(
                starting_data[:, :, mask_where_diffuse]
            )
        )

        # Shape (S, N, L, D). This shape makes sense because we are interested in the tensor N,L,D by slices over S-dim.
        return diffused_starting_data

    def _compute_score_matching_loss(self, targets):
        time_step_diffusion = torch.randint(
            1, self.num_diffusion_steps + 1, (1,), device=self.device
        )
        _, diffusion = self.diffusion_process._compute_drift_and_diffusion(
            torch.zeros_like(targets), time_step_diffusion
        )
        mean, std = self.diffusion_process._perturbation_kernel(
            targets, time_step_diffusion
        )
        noise = torch.randn_like(targets)
        perturbed_noise = mean + std * noise
        pred_score = self.score_network(perturbed_noise, time_step_diffusion)
        # NCSN score matching objective function (x_tilda - x) / sigma^2
        target = -noise / std
        loss_gen_score_matching = (
            diffusion * diffusion * self.reconstruction_loss(pred_score, target)
        )
        return loss_gen_score_matching
