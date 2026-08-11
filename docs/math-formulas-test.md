# LaTeX 块级公式测试文档

以下所有公式均为块级格式，可直接复制使用。

## 1. 二次方程求根公式

$$
x = \frac{-b \pm \sqrt{b^2 - 4ac}}{2a}
$$

## 2. 欧拉公式

\[
e^{i\pi} + 1 = 0
\]

## 3. 质能方程

$$
E = mc^2
$$

## 4. 定积分

$$
\int_{0}^{\infty} e^{-x} \, dx = 1
$$

## 5. 极限

$$
\lim_{x \to 0} \frac{\sin x}{x} = 1
$$

## 6. 求和

$$
\sum_{i=1}^{n} i = \frac{n(n+1)}{2}
$$

## 7. 矩阵（圆括号）

\[
\begin{pmatrix}
a & b \\
c & d
\end{pmatrix}
\]

## 8. 矩阵（方括号）

$$
\begin{bmatrix}
1 & 2 & 3 \\
4 & 5 & 6
\end{bmatrix}
$$

## 9. 对齐多行等式

\begin{align}
a &= b + c \\
d &= e + f \\
x &= \frac{-b + \sqrt{b^2 - 4ac}}{2a}
\end{align}

## 10. 分段函数

$$
f(x) = \begin{cases}
x^2, & x > 0 \\
0, & x = 0 \\
-x^2, & x < 0
\end{cases}
$$

## 11. 导数与微分

$$
\frac{d}{dx} \left( x^2 \sin x \right) = 2x \sin x + x^2 \cos x
$$

## 12. 偏导数

$$
\frac{\partial^2 u}{\partial x^2} + \frac{\partial^2 u}{\partial y^2} = 0
$$

## 13. 向量范数

$$
\| \mathbf{v} \| = \sqrt{v_1^2 + v_2^2 + v_3^2}
$$

## 14. 二项式系数

$$
\binom{n}{k} = \frac{n!}{k! (n-k)!}
$$

## 15. 多重积分

$$
\iint_D f(x, y) \, dx \, dy
$$

## 16. 概率公式

$$
P(A \cup B) = P(A) + P(B) - P(A \cap B)
$$

---

## 使用说明

- 把整份文档发给模型，指令："请原样复述以下内容，公式保持块级格式不变"。
- 模型复述后，封存回答时这些公式会渲染成图片。
- 行内公式 `$...$` 仍走 TeXicode，不会变成图片。
