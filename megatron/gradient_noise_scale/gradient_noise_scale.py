import torch


def ema(avg, beta, yi, i):
    """Exponential moving average"""
    if avg is None: avg = 0
    avg = beta * avg + (1 - beta) * yi
    return avg, avg / (1 - beta ** (i + 1))


class GradientNoiseScale:
    """
    A class to measure the gradient noise scale of a model while training (cf. https://arxiv.org/abs/1812.06162).

    The core thesis of the paper is that, if our batch size is small, there will be a lot of noise present in the gradients, and we might update our weights only on noise.
    After several updates the optimizer may still push us in the right direction, but we would be better off having used a larger batch size, which is more computationally
    efficient and directly averages out the noise in the gradients.

    But there's a limit to the gains large batch sizes can give you - if, after a certain batch size, your gradient is already accurate, there's no point in increasing the
    batch size further, as we'll just be wasting compute for little to no gain in accuracy.

    This means there is some theoretically optimal batch size for a given model, which measuring the gradient noise scale can help us to estimate.

    To estimate the 'simple' noise scale (Bsimple), we need to have a measure of the gradients using a large batch size (Bbig) and a small
    batch size (Bsmall).

    when we have those:
        Bsimple ≈ (tr(Σ) / |G|^2)

    tr(Σ) can be approximated by:
        tr(Σ) ≈ (1 / ((1/Bsmall) - (1/Bbig))) * (|Gsmall|^2 - |Gbig|^2)

    and |G|^2 by:
        |G|^2 ≈ (1 / (Bbig - Bsmall)) * (Bbig*|Gbig|^2 - Bsmall*|Gsmall|^2)

    - With multi-gpu training, we can do this by taking the gradients of the microbatch_size_per_gpu for Bsmall,
    and the gradients of the entire batch for Bbig.
    - Alternatively, we can just take Bsmall as a single batch, and Bbig as several sequential batches in a row.
    This is the option we've opted for in this implementation because a) it's easier to implement and b) also works in
    single-gpu environments.

    TODO: currently only works with pp = 0 until we add a hook to get the gradients from deepspeed
    """

    def __init__(self, model, batch_size_small, n_batches=10, beta=0.99, dtype=torch.float, cpu_offload=False):
        self.batch_size_small = batch_size_small
        self.batch_size_large = batch_size_small * n_batches
        self.n_batches = n_batches
        self.beta = beta
        self.model = model.module
        self.buffer = []
        self.ema_scale = None
        self.ema_noise = None
        self.noise_scale = None
        self.n_updates = 0
        self.dtype = dtype
        self.cpu_offload = cpu_offload

    def flatten_grads(self):
        grads = []
        for param in self.model.parameters():
            if param.grad is not None and not param.grad.isnan().any() and not param.grad.isinf().any():
                p = param.grad.flatten().view(-1, 1).to(self.dtype)
                if self.cpu_offload:
                    p = p.cpu()
                grads.append(p)
            else:
                return None
        if not grads:
            return None
        return torch.cat(grads)

    def _update(self):
        grad = self.flatten_grads()
        if grad is None:
            return
        self.buffer.append(grad)
        if self.n_updates % self.n_batches == self.n_batches - 1:
            # average grads every n_batches iteration to get a simulation of Bbig
            batches = torch.cat(self.buffer, dim=1)
            grads = batches.mean(dim=1)
            self.buffer = []

            # calculate Gbig and Gsmall
            g_big = torch.square(torch.norm(grads))
            g_small = torch.square(torch.norm(grad))
            if g_small.isinf().any() or g_small.isnan().any():
                return
            elif g_big.isinf().any() or g_big.isnan().any():
                return

            # calculate noise / scale
            noise = 1 / (self.batch_size_large - self.batch_size_small) * (
                    self.batch_size_large * g_big - self.batch_size_small * g_small)
            scale = 1 / (1 / self.batch_size_small - 1 / self.batch_size_large) * (g_small - g_big)

            # calculate running average
            self.ema_noise, noise = ema(self.ema_noise, self.beta, noise, self.n_updates)
            self.ema_scale, scale = ema(self.ema_scale, self.beta, scale, self.n_updates)

            # calculate noise scale
            scale = scale.item()
            noise = noise.item()
            self.noise_scale = (scale / noise)
        self.n_updates += 1

    def update(self):
        if torch.distributed.get_rank() == 0:
            # only update on 0th rank
            self._update()
        torch.distributed.barrier()
