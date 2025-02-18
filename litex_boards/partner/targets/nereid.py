#!/usr/bin/env python3

# This file is Copyright (c) 2018-2019 Florent Kermarrec <florent@enjoy-digital.fr>
# This file is Copyright (c) 2018-2019 Rohit Singh <rohit@rohitksingh.in>
# License: BSD

import sys

from migen import *

from litex.build.generic_platform import *
from litex.soc.integration.soc_core import *
from litex.soc.integration.soc_sdram import *
from litex.soc.integration.builder import *
from litex.soc.cores.clock import *
from litex.soc.cores import dna, xadc
from litex.soc.cores.uart import *
from litex.soc.integration.cpu_interface import get_csr_header

from litedram.modules import SDRAMModule
from litedram.modules import _TechnologyTimings, _SpeedgradeTimings
from litedram.phy import s7ddrphy

from litepcie.phy.s7pciephy import S7PCIEPHY
from litepcie.core import LitePCIeEndpoint, LitePCIeMSI
from litepcie.frontend.dma import LitePCIeDMA
from litepcie.frontend.wishbone import LitePCIeWishboneBridge

from litex_boards.platforms import nereid

# sdram module
# --------------------------------------------------------------------------------------------------

class MT8KTF51264(SDRAMModule):
    memtype = "DDR3"
    # geometry
    nbanks = 8
    nrows  = 16384
    ncols  = 1024
    # timings
    technology_timings = _TechnologyTimings(tREFI=64e6/8192, tWTR=(4, 7.5), tCCD=(4, None), tRRD=(4, 10))
    speedgrade_timings = {
        "800": _SpeedgradeTimings(tRP=13.91, tRCD=13.91, tWR=13.91, tRFC=260, tFAW=(None, 50), tRAS=None),
        "1066": _SpeedgradeTimings(tRP=15, tRCD=15, tWR=15, tRFC=86, tFAW=(None, 50), tRAS=None),
        "1333": _SpeedgradeTimings(tRP=15, tRCD=15, tWR=15, tRFC=107, tFAW=(None, 45), tRAS=None),
    }
    speedgrade_timings["default"] = speedgrade_timings["1333"]

# CRG ----------------------------------------------------------------------------------------------

class CRG(Module):
    def __init__(self, platform, sys_clk_freq):
        self.clock_domains.cd_sys = ClockDomain()
        self.clock_domains.cd_sys4x = ClockDomain(reset_less=True)
        self.clock_domains.cd_clk200 = ClockDomain()

        clk100 = platform.request("clk100")

        self.submodules.pll = pll = S7PLL()
        pll.register_clkin(clk100, 100e6)
        pll.create_clkout(self.cd_sys, sys_clk_freq)
        pll.create_clkout(self.cd_sys4x, 4*sys_clk_freq)
        pll.create_clkout(self.cd_clk200, 200e6)
        self.comb += pll.reset.eq(platform.request("cpu_reset"))

        self.submodules.idelayctrl = S7IDELAYCTRL(self.cd_clk200)

# NereidSoC ------------------------------------------------------------------------------------------

class NereidSoC(SoCSDRAM):
    SoCSDRAM.mem_map["csr"] = 0x00000000
    SoCSDRAM.mem_map["rom"] = 0x20000000

    def __init__(self, platform, with_pcie_uart=True):
        sys_clk_freq = int(100e6)

        # soc sdram
        # ------------------------------------------------------------------------------------------
        SoCSDRAM.__init__(self, platform, sys_clk_freq,
            csr_data_width=32,
            integrated_rom_size=0x10000,
            integrated_sram_size=0x10000,
            integrated_main_ram_size=0x10000, # FIXME: keep this for initial PCIe tests
            ident="Nereid LiteX Test SoC", ident_version=True,
            with_uart=not with_pcie_uart)

        # crg
        # ------------------------------------------------------------------------------------------
        self.submodules.crg = CRG(platform, sys_clk_freq)
        self.add_csr("crg")

        # dna
        # ------------------------------------------------------------------------------------------
        self.submodules.dna = dna.DNA()
        self.add_csr("dna")

        # xadc
        # ------------------------------------------------------------------------------------------
        self.submodules.xadc = xadc.XADC()
        self.add_csr("xadc")

        # sdram
        # ------------------------------------------------------------------------------------------
        if not self.integrated_main_ram_size:
            self.submodules.ddrphy = s7ddrphy.K7DDRPHY(
                platform.request("ddram"),
                sys_clk_freq=sys_clk_freq,
                iodelay_clk_freq=200e6)
            sdram_module = MT8KTF51264(sys_clk_freq, "1:4", speedgrade="800")
            self.register_sdram(self.ddrphy,
                                sdram_module.geom_settings,
                                sdram_module.timing_settings)
            self.add_csr("ddrphy")

        # pcie
        # ------------------------------------------------------------------------------------------
        # pcie phy
        self.submodules.pcie_phy = S7PCIEPHY(platform, platform.request("pcie_x1"), bar0_size=0x20000)
        self.pcie_phy.cd_pcie.clk.attr.add("keep")
        platform.add_platform_command("create_clock -name pcie_clk -period 8 [get_nets pcie_clk]")
        platform.add_false_path_constraints(
            self.crg.cd_sys.clk,
            self.pcie_phy.cd_pcie.clk)
        self.add_csr("pcie_phy")

        # pcie endpoint
        self.submodules.pcie_endpoint = LitePCIeEndpoint(self.pcie_phy)

        # pcie wishbone bridge
        self.submodules.pcie_wishbone = LitePCIeWishboneBridge(self.pcie_endpoint, lambda a: 1, shadow_base=self.shadow_base)
        self.add_wb_master(self.pcie_wishbone.wishbone)

        # pcie dma
        self.submodules.pcie_dma = LitePCIeDMA(self.pcie_phy, self.pcie_endpoint,
            with_buffering=True, buffering_depth=1024, with_loopback=True)
        self.add_csr("pcie_dma")

        # pcie msi
        self.submodules.pcie_msi = LitePCIeMSI()
        self.add_csr("pcie_msi")
        self.comb += self.pcie_msi.source.connect(self.pcie_phy.msi)
        self.msis = {
            "DMA_WRITER": self.pcie_dma.writer.irq,
            "DMA_READER": self.pcie_dma.reader.irq
        }
        for i, (k, v) in enumerate(sorted(self.msis.items())):
            self.comb += self.pcie_msi.irqs[i].eq(v)
            self.add_constant(k + "_INTERRUPT", i)

        # pcie_uart
        # ------------------------------------------------------------------------------------------
        if with_pcie_uart:
            class PCIeUART(Module, AutoCSR):
                def __init__(self, uart):
                    self.rx_valid = CSRStatus()
                    self.rx_ready = CSR()
                    self.rx_data = CSRStatus(8)

                    self.tx_valid = CSR()
                    self.tx_ready = CSRStatus()
                    self.tx_data = CSRStorage(8)

                    # # #

                    # cpu to pcie
                    self.comb += [
                        self.rx_valid.status.eq(uart.sink.valid),
                        uart.sink.ready.eq(self.rx_ready.re),
                        self.rx_data.status.eq(uart.sink.data),
                    ]

                    # pcie to cpu
                    self.sync += [
                        If(self.tx_valid.re,
                            uart.source.valid.eq(1)
                        ).Elif(uart.source.ready,
                            uart.source.valid.eq(0)
                        )
                    ]
                    self.comb += [
                        self.tx_ready.status.eq(~uart.source.valid),
                        uart.source.data.eq(self.tx_data.storage)
                    ]

            uart_interface = RS232PHYInterface()
            self.submodules.uart = UART(uart_interface)
            self.add_csr("uart")
            self.add_interrupt("uart")
            self.submodules.pcie_uart = PCIeUART(uart_interface)
            self.add_csr("pcie_uart")

        # leds
        # ------------------------------------------------------------------------------------------
        # led blinking (sys)
        sys_counter = Signal(32)
        self.sync.sys += sys_counter.eq(sys_counter + 1)
        rgb = platform.request("rgb_led")
        self.comb += [
            rgb.r.eq(1),
            rgb.g.eq(sys_counter[26]),
            rgb.b.eq(1),
        ]

    def generate_software_header(self, filename):
        csr_header = get_csr_header(self.get_csr_regions(),
                                    self.get_constants(),
                                    with_access_functions=False,
                                    with_shadow_base=False)
        tools.write_to_file(filename, csr_header)


# Build --------------------------------------------------------------------------------------------

def main():
    platform = nereid.Platform()
    soc = NereidSoC(platform)
    builder = Builder(soc, output_dir="../build/nereid", csr_csv="../build/nereid/csr.csv",
        compile_gateware=not "no-compile" in sys.argv[1:])
    vns = builder.build(build_name="nereid")
    soc.generate_software_header("../software/kernel/csr.h")


if __name__ == "__main__":
    main()
