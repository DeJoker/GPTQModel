# Adapted from turboderp exllama: https://github.com/turboderp/exllama

from exllama_kernels import make_q4, q4_matmul
import torch
import torch.nn as nn
import math

# Dummy tensor to pass instead of g_idx since there is no way to pass "None" to a C++ extension
none_tensor = torch.empty((1, 1), device = "meta")

def ext_make_q4(qweight, qzeros, scales, g_idx, device):
    """Construct Q4Matrix, return handle"""
    return make_q4(qweight,
                   qzeros,
                   scales,
                   g_idx if g_idx is not None else none_tensor,
                   device)

def ext_q4_matmul(x, q4, q4_width):
    """Matrix multiplication, returns x @ q4"""
    outshape = x.shape[:-1] + (q4_width,)
    x = x.view(-1, x.shape[-1])
    output = torch.empty((x.shape[0], q4_width), dtype = torch.float16, device = x.device)

    q4_matmul(x, q4, output)

    return output.view(outshape)


class QuantLinear(nn.Module):
    QUANT_TYPE = "exllama"

    """Linear layer implementation with per-group 4-bit quantization of the weights"""
    def __init__(self,
        bits,
        group_size,
        infeatures,
        outfeatures,
        bias,
        trainable=False,
        **kwargs,
    ):
        super().__init__()
        if bits != 4:
            raise ValueError(f"Exllama kernel supports only bits=4, requested bits={bits}. Something is wrong in the model initialization.")
        
        self.infeatures = infeatures
        self.outfeatures = outfeatures
        self.bits = bits
        self.group_size = group_size if group_size != -1 else infeatures
        self.trainable = trainable
        self.maxq = 2 ** self.bits - 1

        assert infeatures % 32 == 0
        assert infeatures % self.group_size == 0
        assert outfeatures % 32 == 0

        self.register_buffer(
            'qweight',
            torch.zeros((infeatures // 32 * self.bits, outfeatures), dtype=torch.int32)
        )
        self.register_buffer(
            'qzeros',
            torch.zeros((math.ceil(infeatures / self.group_size), outfeatures // 32 * self.bits), dtype=torch.int32)
        )
        self.register_buffer(
            'scales',
            torch.zeros((math.ceil(infeatures / self.group_size), outfeatures), dtype=torch.float16)
        )
        self.register_buffer(
            'g_idx',
            torch.tensor([i // self.group_size for i in range(infeatures)], dtype=torch.int32)
        )

        if bias:
            self.register_buffer('bias', torch.zeros((outfeatures), dtype=torch.float16))
        else:
            self.bias = None

    def post_init(self):
        assert self.qweight.device.type == "cuda"
        assert self.qweight.device.index is not None
        
        self.width = self.qweight.shape[1]

        self.q4 = ext_make_q4(
            self.qweight,
            self.qzeros,
            self.scales,
            self.g_idx,
            self.qweight.device.index
        )


    def pack(self, linear, scales, zeros, g_idx=None):
        raise NotImplementedError("Pack is not supported for the exllama implementation. Please open an issue.")

    def forward(self, x):
        out = ext_q4_matmul(x, self.q4, self.width)

        if self.bias is not None:
            out.add_(self.bias)
        return out
